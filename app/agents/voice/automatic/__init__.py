import asyncio
import argparse
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo

from opentelemetry import trace
from langfuse import get_client

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.filters.noisereduce_filter import NoisereduceFilter
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.rtvi import GoogleRTVIObserver
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TTSSpeakFrame, BotSpeakingFrame, LLMFullResponseEndFrame
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIProcessor

from app.core import config
from app.core.logger import logger, configure_session_logger
from app.services.speaker_diarization import speaker_diarization_service, SpeakerDiarizationConfig
from app.utils.session_context import create_session_context
from app.agents.voice.automatic.services.llm_wrapper import LLMServiceWrapper
from app.agents.voice.automatic.services.mcp.automatic_client import MCPClient
from app.agents.voice.automatic.analytics.tracing_setup import setup_tracing
from .processors import LLMSpyProcessor
from .prompts import get_system_prompt
from .tools import initialize_tools
from .services.mock_stt import TestQuestionProcessor, DEFAULT_TEST_QUESTIONS
from .tts import get_tts_service
from .types import (
    TTSProvider,
    Mode,
    decode_tts_provider,
    decode_voice_name,
    decode_mode,
)

load_dotenv(override=True)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, required=True, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, required=True, help="Daily token")
    parser.add_argument("--mode", type=str, help="Mode (TEST or LIVE)")
    parser.add_argument("--session-id", type=str, required=True, help="Session ID for logging")
    parser.add_argument("--euler-token", type=str, help="Euler token for live mode")
    parser.add_argument("--breeze-token", type=str, help="Breeze token for live mode")
    parser.add_argument("--shop-url", type=str, help="Shop URL for live mode")
    parser.add_argument("--shop-id", type=str, help="Shop ID for live mode")
    parser.add_argument("--shop-type", type=str, help="Shop type for live mode")
    parser.add_argument("--user-name", type=str, help="User's name")
    parser.add_argument("--tts-provider", type=str, help="TTS provider to use")
    parser.add_argument("--voice-name", type=str, help="Voice name to use")
    parser.add_argument("--merchant-id", type=str, help="Merchant Id of the Shop")
    parser.add_argument("--platform-integrations",type=str, nargs="+", help="Platform Integrations that are supported by the shop (string array)")
    args = parser.parse_args()

    # Configure logger with session ID for all logs in this subprocess
    configure_session_logger(args.session_id)
    logger.info(f"Voice agent started with session ID: {args.session_id}")
    
    # Create session context for passing to components
    session_context = create_session_context(args.session_id)

    # Decode TTS parameters
    tts_provider = decode_tts_provider(args.tts_provider)
    voice_name = decode_voice_name(args.voice_name)
    mode = decode_mode(args.mode)

    # Initialize tools based on the mode and provided tokens
    # Only pass tokens if in live mode
    
    use_automatic_mcp_server = config.AUTOMATIC_MCP_TOOL_SERVER_USAGE or \
        (args.shop_id and args.shop_id in config.SHOPS_FOR_AUTOMATIC_MCP_SERVER)

    # Personalize the system prompt if a user name is provided
    system_prompt = get_system_prompt(args.user_name, tts_provider)

    # Create VAD analyzer with a reference for real-time updates
    vad_analyzer = SileroVADAnalyzer(
        sample_rate=16000,
        params=VADParams(
            confidence=config.VAD_CONFIDENCE,
            start_secs=0.30,
            stop_secs=1.00,
            min_volume=config.VAD_MIN_VOLUME,
        )
    )
    
    # Log initial VAD configuration
    logger.info(f"🎯 VAD ANALYZER INITIALIZED - ID: {id(vad_analyzer)}, Params ID: {id(vad_analyzer._params)}")
    logger.info(f"🎯 Initial VAD params: confidence={vad_analyzer._params.confidence}, start_secs={vad_analyzer._params.start_secs}, stop_secs={vad_analyzer._params.stop_secs}, min_volume={vad_analyzer._params.min_volume}")
    
    # Create a wrapper to log VAD usage periodically
    class VADLoggingWrapper:
        def __init__(self, original_vad):
            self.original_vad = original_vad
            self._params = original_vad._params  # Expose the same _params for updates
            self.call_count = 0
            
        def analyze_audio(self, audio_data):
            self.call_count += 1
            
            # Log current parameters every 500 calls (~10 seconds of audio)
            if self.call_count % 500 == 0:
                logger.info(f"🔊 VAD PROCESSING - Call #{self.call_count}, Using params: confidence={self._params.confidence}, start_secs={self._params.start_secs}, stop_secs={self._params.stop_secs}, min_volume={self._params.min_volume}")
            
            # Call original VAD analyzer
            return self.original_vad.analyze_audio(audio_data)
            
        def __getattr__(self, name):
            # Delegate all other attributes to original VAD
            return getattr(self.original_vad, name)
    
    # Wrap the VAD analyzer for logging
    vad_analyzer = VADLoggingWrapper(vad_analyzer)
    
    daily_params = DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=vad_analyzer,
    )

    if config.ENABLE_NOISE_REDUCE_FILTER:
        daily_params.audio_in_filter = NoisereduceFilter()

    transport = DailyTransport(
        args.url,
        args.token,
        "Breeze Automatic Voice Agent",
        daily_params,
    )
    
    # Log object reference verification
    logger.info(f"🔍 OBJECT REFERENCE VERIFICATION:")
    logger.info(f"  - Our VAD analyzer ID: {id(vad_analyzer)}")
    logger.info(f"  - DailyParams VAD ID: {id(daily_params.vad_analyzer)}")
    logger.info(f"  - Same object? {vad_analyzer is daily_params.vad_analyzer}")
    logger.info(f"  - VAD params object ID: {id(vad_analyzer._params)}")
    logger.info(f"🚀 All components will use the SAME VAD analyzer object for real-time updates")

    # Choose STT service based on speaker diarization configuration
    if config.ENABLE_SPEAKER_DIARIZATION and config.SPEECHMATICS_API_KEY:
        # Create configuration for speaker diarization
        diarization_config = SpeakerDiarizationConfig(
            enable_diarization=config.ENABLE_SPEAKER_DIARIZATION,
            enable_voice_locking=config.ENABLE_VOICE_LOCKING,
            speaker_sensitivity=config.SPEAKER_SENSITIVITY,
            max_speakers=config.MAX_SPEAKERS
        )
        
        # Use the improved service with automatic fallback
        stt, voice_locking_processor = speaker_diarization_service.create_stt_with_fallback(
            config=diarization_config,
            languages=[Language.EN_US, Language.EN_IN]
        )
    else:
        # Create Google STT directly when speaker diarization is disabled
        stt = speaker_diarization_service.create_google_stt_fallback(
            languages=[Language.EN_US, Language.EN_IN]
        )
        voice_locking_processor = None
        logger.info("Using Google STT (speaker diarization disabled)")

    llm = LLMServiceWrapper(AzureLLMService(
        api_key=config.AZURE_OPENAI_API_KEY,
        endpoint=config.AZURE_OPENAI_ENDPOINT,
        model=config.AZURE_OPENAI_MODEL,
    ))

    if not use_automatic_mcp_server:
        if mode == Mode.LIVE:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                breeze_token=args.breeze_token,
                euler_token=args.euler_token,
                shop_url=args.shop_url,
                shop_id=args.shop_id,
                shop_type=args.shop_type,
                merchant_id=args.merchant_id,
            )
        else:
            tools, tool_functions = initialize_tools(
                mode=mode.value,
                merchant_id=args.merchant_id,
            )
            
        for name, function in tool_functions.items():
            logger.info("Initializing the default function tools")
            llm.register_function(name, function)
    else:
        logger.info(f"Initializing tools from remote MCP server")
        
        mcp_context = {
            "sessionId": args.session_id,
            "juspayToken": args.euler_token,
            "shopUrl": args.shop_url,
            "shopId": args.shop_id,
            "shopType": args.shop_type,
            "userId": args.user_name,
            "enableDemoMode": mode != Mode.LIVE,
            "merchantId": args.merchant_id,
            "platformIntegrations": args.platform_integrations
        }
        mcp_client = MCPClient(
            server_url=config.AUTOMATIC_TOOL_MCP_SERVER_URL,
            auth_token=args.breeze_token,
            context=mcp_context,
            session_context=session_context
        )
        tools = await mcp_client.register_tools(llm)


    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    tts = get_tts_service(
        tts_provider=tts_provider.value, 
        voice_name=voice_name.value,
        session_id=args.session_id
    )
    # Simplified event handler for TTS feedback
    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        del service  # Unused parameter
        # Only play the "checking" message if using Google TTS
        if tts_provider == TTSProvider.GOOGLE:
            for function_call in function_calls:
                if function_call.function_name != "get_current_time":
                    await tts.queue_frame(TTSSpeakFrame("Let me check on that."))
                    break

    messages = [
        {
            "role": "system",
            "content": system_prompt
        },
    ]

    context = llm.create_summarizing_context(
        messages,
        tools,
    )

    context_aggregator = llm.create_context_aggregator(context)

    # Add custom LLMSpyProcessor for streaming function call events (RTVI and TTS created earlier)
    tool_call_processor = LLMSpyProcessor(rtvi, args.session_id)

    # Build pipeline components
    pipeline_components = [
        transport.input(),
        stt,
    ]
    
    if config.ENVIRONMENT.lower() in ["development", "dev"]:
        test_processor = TestQuestionProcessor(questions=DEFAULT_TEST_QUESTIONS)
        pipeline_components.append(test_processor)
        logger.info("Test Question Processor enabled (development mode)")

    if voice_locking_processor is not None:
        pipeline_components.append(voice_locking_processor)
        logger.info("Added voice locking processor to pipeline")
    
    pipeline_components.extend([
        context_aggregator.user(),
        llm,
        tool_call_processor,
        rtvi,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    
    pipeline = Pipeline(pipeline_components)

    user_name = args.user_name or "guest"
    shopId = "euler" if args.euler_token and not args.shop_id else args.shop_id or "dummy"
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y-%m-%d_%H-%M-%S")
    conversation_id=f"{user_name}-{shopId}-{timestamp}"

    task_params = {
        "idle_timeout_secs": 180.0,
        "idle_timeout_frames": (BotSpeakingFrame, LLMFullResponseEndFrame),
        "params": PipelineParams(allow_interruptions=True),
        "cancel_on_idle_timeout": True,
        "observers": [GoogleRTVIObserver(rtvi)],
    }

    if config.ENABLE_TRACING:
        setup_tracing("breeze-voice-agent")
        task_params["conversation_id"] = conversation_id
        task_params["enable_tracing"] = True

    task = PipelineTask(pipeline, **task_params)

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        del rtvi
        await rtvi.set_bot_ready()

    @rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, message):
        """Handle custom client messages for real-time parameter updates."""
        try:
            action = message.get("action")
            data = message.get("data", {})
            
            if action == "update_vad_params":
                await update_vad_parameters(rtvi, vad_analyzer, data)
            else:
                await rtvi.send_message({
                    "type": "error",
                    "message": f"Unknown action: {action}"
                })
        except Exception as e:
            logger.error(f"Error handling client message: {e}")
            await rtvi.send_message({
                "type": "error", 
                "message": f"Error processing message: {str(e)}"
            })

    async def update_vad_parameters(rtvi, vad_analyzer, params):
        """Update VAD analyzer parameters in real-time with validation."""
        try:
            current_params = vad_analyzer._params
            
            # Log current state before update
            logger.info(f"🎯 VAD UPDATE REQUEST - Current params: confidence={current_params.confidence}, start_secs={current_params.start_secs}, stop_secs={current_params.stop_secs}, min_volume={current_params.min_volume}")
            logger.info(f"🔍 VAD OBJECT VERIFICATION - VAD analyzer ID: {id(vad_analyzer)}, VAD params ID: {id(current_params)}")
            
            # Validate parameter ranges
            valid_params = {}
            
            if "confidence" in params:
                confidence = float(params["confidence"])
                if 0.0 <= confidence <= 1.0:
                    valid_params["confidence"] = confidence
                else:
                    raise ValueError("Confidence must be between 0.0 and 1.0")
            
            if "start_secs" in params:
                start_secs = float(params["start_secs"])
                if 0.0 <= start_secs <= 5.0:
                    valid_params["start_secs"] = start_secs
                else:
                    raise ValueError("start_secs must be between 0.0 and 5.0")
            
            if "stop_secs" in params:
                stop_secs = float(params["stop_secs"])
                if 0.1 <= stop_secs <= 10.0:
                    valid_params["stop_secs"] = stop_secs
                else:
                    raise ValueError("stop_secs must be between 0.1 and 10.0")
            
            if "min_volume" in params:
                min_volume = float(params["min_volume"])
                if 0.0 <= min_volume <= 1.0:
                    valid_params["min_volume"] = min_volume
                else:
                    raise ValueError("min_volume must be between 0.0 and 1.0")
            
            if not valid_params:
                raise ValueError("No valid parameters provided")
            
            # Update VAD analyzer parameters with detailed logging
            for param, new_value in valid_params.items():
                old_value = getattr(current_params, param)
                setattr(current_params, param, new_value)
                logger.info(f"✅ VAD PARAMETER UPDATED: {param} changed from {old_value} → {new_value}")
            
            # Verify the changes took effect
            logger.info(f"🎯 VAD UPDATE COMPLETE - New params: confidence={current_params.confidence}, start_secs={current_params.start_secs}, stop_secs={current_params.stop_secs}, min_volume={current_params.min_volume}")
            logger.info(f"🚀 VAD analyzer is now using updated parameters for subsequent audio processing")
            
            # Send confirmation back to client
            await rtvi.send_message({
                "type": "vad_params_updated",
                "data": {
                    "updated_params": valid_params,
                    "current_params": {
                        "confidence": current_params.confidence,
                        "start_secs": current_params.start_secs,
                        "stop_secs": current_params.stop_secs,
                        "min_volume": current_params.min_volume
                    }
                }
            })
            
        except Exception as e:
            logger.error(f"❌ Error updating VAD parameters: {e}")
            await rtvi.send_message({
                "type": "error",
                "message": f"Failed to update VAD parameters: {str(e)}"
            })

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        del transport
        logger.info(f"First participant joined: {participant['id']}")
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        del transport, reason
        logger.info(f"Participant left: {participant['id']}")
        await task.cancel()

    @task.event_handler("on_pipeline_cancelled")
    async def on_pipeline_cancelled(task_obj, frame):
        del task_obj, frame  # Unused but required by event handler signature
        logger.info("Pipeline task cancelled. Cancelling main task.")
        main_task = asyncio.current_task()
        main_task.cancel()

    runner = PipelineRunner()

    async def run_pipeline():
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            logger.info("Main task cancelled. Exiting gracefully.")
        except Exception as e:
            logger.error(f"Pipeline runner error: {e}")

    if config.ENABLE_TRACING:
        langfuse_client = get_client()
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(conversation_id) as root_span:
            logger.info(f"Starting current span with conversation ID: {conversation_id}")
            root_span.set_attribute("conversation.id", conversation_id)
            root_span.set_attribute("conversation.type", "voice")
            root_span.set_attribute("user.name", user_name)
            root_span.set_attribute("service.name", "breeze-voice-agent")
            langfuse_client.update_current_trace(user_id=user_name)
            langfuse_client.update_current_trace(session_id=args.session_id)
            langfuse_client.update_current_trace(tags=[voice_name])
            await run_pipeline()
    else:
        await run_pipeline()
