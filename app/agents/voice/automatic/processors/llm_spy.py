"""
LLM Spy Processor for intercepting function calls and conversation events.
Lightweight frame processor that delegates business logic to ConversationManager.
"""

import time
import asyncio
from typing import Dict, Optional

from pipecat.frames.frames import (
    Frame, 
    FunctionCallInProgressFrame, 
    FunctionCallResultFrame, 
    LLMTextFrame,
    LLMFullResponseStartFrame, 
    LLMFullResponseEndFrame
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.core.logger import logger


# Global RTVI processor reference for function confirmations
_rtvi_processor = None

# Global storage for pending confirmations with thread safety
import threading
_pending_confirmations: Dict[str, asyncio.Future] = {}
_confirmations_lock = threading.Lock()

def get_rtvi_processor():
    """Get the global RTVI processor instance for function confirmations"""
    return _rtvi_processor

def set_rtvi_processor(rtvi):
    """Set the global RTVI processor instance"""
    global _rtvi_processor
    _rtvi_processor = rtvi

def register_pending_confirmation(confirmation_id: str) -> None:
    """Register a pending confirmation that awaits user response via RTVI"""
    global _pending_confirmations
    with _confirmations_lock:
        _pending_confirmations[confirmation_id] = asyncio.Future()
        logger.info(f"Registered pending confirmation: {confirmation_id}")

async def wait_for_confirmation_response(confirmation_id: str, timeout_seconds: int = 30) -> Optional[Dict]:
    """Wait for confirmation response via RTVI events"""
    global _pending_confirmations
    
    with _confirmations_lock:
        if confirmation_id not in _pending_confirmations:
            logger.error(f"No pending confirmation found for ID: {confirmation_id}")
            return None
        future = _pending_confirmations[confirmation_id]
    
    try:
        logger.info(f"Waiting for confirmation response {confirmation_id} with timeout {timeout_seconds}s")
        # Wait for the response with timeout
        response = await asyncio.wait_for(future, timeout=timeout_seconds)
        logger.info(f"Received confirmation response for {confirmation_id}: {response}")
        return response
    except asyncio.TimeoutError:
        logger.warning(f"Confirmation timeout for {confirmation_id} after {timeout_seconds}s")
        return {"approved": False, "reason": "timeout"}
    except Exception as e:
        logger.error(f"Error waiting for confirmation {confirmation_id}: {e}")
        return {"approved": False, "reason": "error"}
    finally:
        # Clean up the pending confirmation
        with _confirmations_lock:
            removed = _pending_confirmations.pop(confirmation_id, None)
            if removed:
                logger.info(f"Cleaned up confirmation {confirmation_id}")

def handle_confirmation_response(confirmation_id: str, response: Dict) -> None:
    """Handle incoming confirmation response from RTVI"""
    global _pending_confirmations
    
    with _confirmations_lock:
        if confirmation_id in _pending_confirmations:
            future = _pending_confirmations[confirmation_id]
            if not future.done():
                future.set_result(response)
                logger.info(f"Set confirmation response for {confirmation_id}: {response}")
            else:
                logger.warning(f"Confirmation {confirmation_id} already completed")
        else:
            logger.warning(f"Received response for unknown confirmation: {confirmation_id}")

# Custom LLMSpyProcessor for streaming function call events
class LLMSpyProcessor(FrameProcessor):
    """
    Lightweight frame processor for intercepting LLM conversation events.
    
    Responsibilities:
    1. Intercepts function call frames and emits RTVI events
    2. Collects LLM responses and delegates to ConversationManager
    3. Handles chart component emission
    4. Processes highlight text for timing correlation
    5. Handles function confirmation responses via RTVI
    """

    def __init__(self, rtvi: RTVIProcessor, session_id: str, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._session_id = session_id
        
        # Register this RTVI processor globally for function confirmations
        set_rtvi_processor(rtvi)
        
        # LLM response collection
        self._accumulated_text = ""
        self._is_collecting_response = False
        

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, FunctionCallInProgressFrame):
            logger.info(f"Function call started: {frame.function_name} with args: {frame.arguments}")
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-start",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "timestamp": int(time.time() * 1000)
                        }
                    }
                )
            )
        elif isinstance(frame, FunctionCallResultFrame):
            logger.info(f"Function call result: {frame.function_name} with result: {frame.result}")
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-result",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "result": frame.result,
                            "timestamp": int(time.time() * 1000)
                        }
                    }
                )
            )

        await self.push_frame(frame, direction)
    
    async def _emit_rtvi_event(self, event) -> None:
        """Emit conversation event via RTVI."""
        try:
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": event.type,
                        "payload": event.payload
                    }
                )
            )
        except Exception as e:
            logger.error(f"Error emitting RTVI event for session {self._session_id}: {e}")

    async def _emit_chart_components(self, function_name: str) -> None:
        """Emit chart components via RTVI frames after function calls."""
        del function_name  # Unused parameter
        try:
            from app.tools.providers.system.chart_tools import get_pending_chart_emissions
            
            pending_charts = get_pending_chart_emissions(self._session_id)
            
            for chart_data in pending_charts:
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-component",
                            "payload": chart_data
                        }
                    )
                )
                
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"Error emitting chart components for session {self._session_id}: {e}")

    async def _handle_confirmation_response(self, event_data: Dict) -> None:
        """Handle function confirmation response events from RTVI client"""
        try:
            confirmation_id = event_data.get("confirmationId")
            approved = event_data.get("approved", False)
            reason = event_data.get("reason", "")
            
            if not confirmation_id:
                logger.error("Received confirmation response without confirmationId")
                return
            
            response = {
                "approved": approved,
                "reason": reason
            }
            
            # Route the response to the waiting confirmation
            handle_confirmation_response(confirmation_id, response)
            
            logger.info(f"Processed confirmation response for {confirmation_id}: approved={approved}")
            
        except Exception as e:
            logger.error(f"Error handling confirmation response: {e}")
