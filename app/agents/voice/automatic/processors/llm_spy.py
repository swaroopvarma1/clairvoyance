import time
import re
from typing import List, Dict, Any, Optional

from app.core.logger import logger
from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, FunctionCallResultFrame, LLMTextFrame, LLMFullResponseEndFrame, TTSTextFrame
from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, FunctionCallResultFrame, LLMMessagesFrame, LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

# Import conversation management
from app.core.conversation_manager import get_conversation_manager
from app.types.conversation import ConversationEvent


# Custom LLMSpyProcessor for streaming function call events and conversation debugging
class LLMSpyProcessor(FrameProcessor):
    """Intercepts LLM conversation frames and provides comprehensive debugging capabilities.
    
    This processor:
    1. Captures complete conversation flow (user messages, LLM responses, tool calls/results)
    2. Emits RTVI server messages for function call start and result events
    3. Emits conversation debugging events for frontend debug panel
    4. Handles chart component emission after chart generation functions
    5. Extracts highlights from LLM text and correlates with ElevenLabs word timing
    6. Emits precise highlight events when trigger words are spoken
    """

    def __init__(self, rtvi: RTVIProcessor, session_id: str, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._session_id = session_id  # Real session ID from voice agent
        
        # LLM response collection
        self._accumulated_text = ""  # Store accumulated LLM response text
        self._is_collecting_response = False  # Track if we're between start and end frames
        
        # Conversation management
        self._conversation_manager = get_conversation_manager()
        self._current_turn_id: Optional[str] = None
        
        # ElevenLabs word timing for highlights
        self._pending_highlights: List[Dict[str, Any]] = []
        self._current_text_clean = ""
        self._highlight_pattern = re.compile(
            r'<highlight\s+category=["\']([^"\']+)["\'][^>]*>(.*?)</highlight>',
            re.IGNORECASE | re.DOTALL
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process LLM conversation frames for debugging and emit RTVI events."""
        
        # Handle all frames normally (no consumption)
        await super().process_frame(frame, direction)
        
        # Handle ElevenLabs word timing for precise highlights
        if isinstance(frame, TTSTextFrame):
            logger.info(f"[{self._session_id}] 🔊 TTSTextFrame: word='{frame.text}', pts={frame.pts}")
            await self._check_word_for_highlights(frame.text, frame.pts)

        # LLM Response Start - begin collecting text AND start conversation turn
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._is_collecting_response = True
            self._accumulated_text = ""
            logger.info(f"[{self._session_id}] 🧠 LLM response started")
            
            # Since we can't intercept LLMMessagesFrame (it's processed before us in pipeline),
            # we'll start conversation turn when LLM response begins (indicates user just spoke)
            await self._start_conversation_turn_from_response_start()
            
        # LLM Output (Response from LLM) - accumulate streaming text only during response
        elif isinstance(frame, LLMTextFrame) and self._is_collecting_response:
            self._accumulated_text += frame.text
            
        # LLM Response Complete - finalize response and potentially complete turn
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._accumulated_text.strip():
                logger.info(f"[{self._session_id}] 🧠 LLM Output: '{self._accumulated_text.strip()}'")
                
                # Add LLM response to conversation
                await self._add_llm_response(self._accumulated_text.strip())
                
            self._accumulated_text = ""
            self._is_collecting_response = False

        # Tool Events - Track function calls and results
        elif isinstance(frame, FunctionCallInProgressFrame):
            logger.info(f"[{self._session_id}] 🔧 Function call started: {frame.function_name}")
            
            # Add tool call to conversation
            await self._add_tool_call(frame.function_name, frame.arguments, frame.tool_call_id)
            
            # Emit existing tool-call-start event for backward compatibility
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
            logger.info(f"[{self._session_id}] 🔧 Function call result: {frame.function_name}")
            
            # Add tool result to conversation
            await self._add_tool_result(frame.tool_call_id, frame.function_name, frame.result)
            
            # Emit existing tool-call-result event for backward compatibility
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
            
            # Check if this was a chart generation function and emit chart components
            if frame.function_name in ["generate_bar_chart", "generate_line_chart", "generate_donut_chart"]:
                await self._emit_chart_components(frame.function_name)

        await self.push_frame(frame, direction)

    async def _emit_chart_components(self, function_name: str):
        """Emit chart components via RTVI frames after chart generation functions"""
        try:
            from app.tools.providers.system.chart_tools import get_pending_chart_emissions
            
            pending_charts = get_pending_chart_emissions(self._session_id)
            
            for chart_data in pending_charts:
                logger.info(f"[{self._session_id}] 🚀 Emitting chart component via RTVI: {chart_data['componentId']}")
                
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-component",
                            "payload": chart_data
                        }
                    )
                )
                
                logger.info(f"[{self._session_id}] ✅ Successfully emitted chart component: {chart_data['componentType']}")
                
        except Exception as e:
            logger.error(f"Error emitting chart components: {e}")

    # Conversation Management Methods
    
    async def _start_conversation_turn(self, user_content: str):
        """Start a new conversation turn with user message"""
        try:
            # Add user message and start new turn
            user_message = self._conversation_manager.add_user_message(self._session_id, user_content)
            conversation = self._conversation_manager.get_conversation(self._session_id)
            turn = conversation.current_turn if conversation else None
            
            if turn:
                self._current_turn_id = turn.id
                
                # Emit conversation turn start event
                turn_start_event = ConversationEvent.turn_start(self._session_id, turn)
                await self._emit_conversation_event(turn_start_event)
                
                logger.info(f"[{self._session_id}] 💬 Started conversation turn: {turn.turn_number}")
            
        except Exception as e:
            logger.error(f"[{self._session_id}] Error starting conversation turn: {e}")
    
    async def _start_conversation_turn_from_response_start(self):
        """Start conversation turn when LLM response begins (pipeline workaround)"""
        try:
            # Since LLMMessagesFrame is processed before us, we infer user input from response start
            user_message = self._conversation_manager.add_user_message(self._session_id, "[Inferred from voice]")
            conversation = self._conversation_manager.get_conversation(self._session_id)
            turn = conversation.current_turn if conversation else None
            
            if turn:
                self._current_turn_id = turn.id
                turn_start_event = ConversationEvent.turn_start(self._session_id, turn)
                await self._emit_conversation_event(turn_start_event)
                
                logger.info(f"[{self._session_id}] 💬 Started conversation turn from LLM response start: {turn.turn_number}")
            
        except Exception as e:
            logger.error(f"[{self._session_id}] Error starting conversation turn from response start: {e}")
    
    async def _add_llm_response(self, response_content: str):
        """Add LLM response to current turn"""
        try:
            assistant_message = self._conversation_manager.add_assistant_message(self._session_id, response_content)
            
            if assistant_message:
                # Emit conversation update event
                conversation = self._conversation_manager.get_conversation(self._session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    update_event = ConversationEvent.turn_update(self._session_id, turn, "llm_response")
                    await self._emit_conversation_event(update_event)
                    
                    logger.info(f"[{self._session_id}] 💬 Added LLM response to turn {turn.turn_number}")
                    
        except Exception as e:
            logger.error(f"[{self._session_id}] Error adding LLM response: {e}")
    
    async def _add_tool_call(self, function_name: str, arguments: Dict[str, Any], tool_call_id: str):
        """Add tool call to current turn"""
        try:
            tool_call = self._conversation_manager.add_tool_call(
                self._session_id, function_name, arguments, tool_call_id
            )
            
            if tool_call:
                # Emit conversation update event
                conversation = self._conversation_manager.get_conversation(self._session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    update_event = ConversationEvent.turn_update(self._session_id, turn, "tool_call")
                    await self._emit_conversation_event(update_event)
                    
                    logger.info(f"[{self._session_id}] 💬 Added tool call {function_name} to turn {turn.turn_number}")
                    
        except Exception as e:
            logger.error(f"[{self._session_id}] Error adding tool call: {e}")
    
    async def _add_tool_result(self, tool_call_id: str, function_name: str, result: str):
        """Add tool result to current turn"""
        try:
            # Determine if the result indicates success or failure
            success = not ("error" in result.lower() or "failed" in result.lower())
            
            tool_result = self._conversation_manager.add_tool_result(
                self._session_id, tool_call_id, function_name, result, success
            )
            
            if tool_result:
                # Emit conversation update event
                conversation = self._conversation_manager.get_conversation(self._session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    update_event = ConversationEvent.turn_update(self._session_id, turn, "tool_result")
                    await self._emit_conversation_event(update_event)
                    
                    # Check if this turn is ready to be completed (has LLM response and all tools are done)
                    await self._check_turn_completion(turn)
                    
                    logger.info(f"[{self._session_id}] 💬 Added tool result for {function_name} to turn {turn.turn_number}")
                    
        except Exception as e:
            logger.error(f"[{self._session_id}] Error adding tool result: {e}")
    
    async def _check_turn_completion(self, turn):
        """Check if turn should be completed and emit completion event"""
        try:
            # Turn is complete if we have:
            # 1. LLM response 
            # 2. All tool calls have results (no pending tool calls)
            if turn.assistant_response and len(turn.tool_calls) == len(turn.tool_results):
                completed_turn = self._conversation_manager.complete_turn(self._session_id)
                
                if completed_turn:
                    # Emit turn complete event
                    complete_event = ConversationEvent.turn_complete(self._session_id, completed_turn)
                    await self._emit_conversation_event(complete_event)
                    
                    # Reset current turn tracking
                    self._current_turn_id = None
                    
                    logger.info(f"[{self._session_id}] 💬 Completed conversation turn {completed_turn.turn_number}")
                    
        except Exception as e:
            logger.error(f"[{self._session_id}] Error checking turn completion: {e}")
    
    async def _emit_conversation_event(self, event: ConversationEvent):
        """Emit conversation event via RTVI"""
        try:
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": event.type,
                        "payload": event.payload
                    }
                )
            )
            
            logger.debug(f"[{self._session_id}] 📡 Emitted conversation event: {event.type}")
            
        except Exception as e:
            logger.error(f"[{self._session_id}] Error emitting conversation event: {e}")

    # ElevenLabs Word Timing Methods
    
    async def _process_highlight_text(self, text: str):
        """Extract highlights from LLM text and store for ElevenLabs word timing correlation."""
        try:
            # Extract highlights from XML tags
            highlights = self._extract_highlights(text)
            clean_text = self._remove_highlight_tags(text)
            
            if highlights:
                # Store for word timing correlation
                self._pending_highlights = self._map_highlights_to_words(highlights, clean_text)
                self._current_text_clean = clean_text
                
                logger.info(f"[{self._session_id}] 📦 Stored {len(highlights)} highlights for ElevenLabs timing")
                logger.debug(f"[{self._session_id}] Clean text: '{clean_text[:100]}...'")
            else:
                # Clear pending highlights if no highlights in this text
                self._pending_highlights = []
                
        except Exception as e:
            logger.error(f"[{self._session_id}] Error processing highlight text: {e}")
    
    def _extract_highlights(self, text: str) -> List[Dict[str, Any]]:
        """Extract highlight information from XML tags in text."""
        highlights = []
        
        for match in self._highlight_pattern.finditer(text):
            category = match.group(1).strip()
            spoken_text = match.group(2).strip()
            
            # Get chart context for validation (with fallback)
            chart_context = self._get_latest_chart_context()
            
            if chart_context:
                categories = chart_context.get('categories', [])
                if category in categories:
                    category_index = categories.index(category)
                    chart_id = chart_context.get('chartId', 'unknown')
                else:
                    # Fallback: Trust LLM's category choice
                    logger.warning(f"[{self._session_id}] Category '{category}' not in stored context, using fallback")
                    category_index = -1
                    chart_id = chart_context.get('chartId', 'latest')
            else:
                # No chart context - create highlight anyway
                logger.warning(f"[{self._session_id}] No chart context, creating fallback highlight")
                category_index = -1
                chart_id = 'latest'
            
            highlight_data = {
                'category': category,
                'spokenText': spoken_text,
                'categoryIndex': category_index,
                'chartId': chart_id,
                'timestamp': int(time.time() * 1000),
                'textPosition': match.start()
            }
            
            highlights.append(highlight_data)
            logger.debug(f"[{self._session_id}] Extracted highlight: {category} -> '{spoken_text}'")
        
        return highlights
    
    def _remove_highlight_tags(self, text: str) -> str:
        """Remove all highlight XML tags from text, keeping only the inner content."""
        return self._highlight_pattern.sub(r'\2', text).strip()
    
    def _map_highlights_to_words(self, highlights: List[Dict[str, Any]], clean_text: str) -> List[Dict[str, Any]]:
        """Map highlights to specific trigger words for ElevenLabs timing correlation."""
        mapped_highlights = []
        clean_words = clean_text.lower().split()
        
        for highlight in highlights:
            # Get trigger words from spoken text
            trigger_words = [word.strip().lower() for word in highlight['spokenText'].lower().split()]
            
            # Find positions of trigger words in clean text
            trigger_positions = []
            for trigger_word in trigger_words:
                for i, clean_word in enumerate(clean_words):
                    # Fuzzy matching - trigger word can be part of clean word
                    if trigger_word in clean_word or clean_word in trigger_word:
                        trigger_positions.append(i)
                        break
            
            if trigger_positions:
                mapped_highlights.append({
                    **highlight,
                    'triggerWords': trigger_words,
                    'triggerPositions': trigger_positions,
                    'triggered': False,
                    'wordCount': len(clean_words)
                })
                
                logger.debug(f"[{self._session_id}] Mapped highlight '{highlight['category']}' to words: {trigger_words}")
        
        return mapped_highlights
    
    async def _check_word_for_highlights(self, word: str, timestamp_ns: int):
        """Check if current word from ElevenLabs should trigger any pending highlights."""
        if not self._pending_highlights or not word:
            return
            
        word_lower = word.lower().strip()
        timestamp_ms = timestamp_ns / 1_000_000  # Convert nanoseconds to milliseconds
        
        for highlight in self._pending_highlights:
            if not highlight['triggered']:
                # Check if this word triggers the highlight
                for trigger_word in highlight['triggerWords']:
                    if trigger_word in word_lower or word_lower in trigger_word:
                        await self._emit_precise_highlight(highlight, timestamp_ms, word)
                        highlight['triggered'] = True
                        break
    
    async def _emit_precise_highlight(self, highlight: Dict[str, Any], timestamp_ms: float, trigger_word: str):
        """Emit highlight with ElevenLabs precise timing."""
        try:
            highlight_event = {
                "type": "voice-highlight",
                "payload": {
                    'category': highlight['category'],
                    'spokenText': highlight['spokenText'],
                    'categoryIndex': highlight['categoryIndex'],
                    'chartId': highlight['chartId'],
                    'timestamp': int(timestamp_ms),
                    'preciseTimestamp': timestamp_ms,
                    'timingSource': 'elevenlabs_exact',
                    'triggerWord': trigger_word
                }
            }
            
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(data=highlight_event)
            )
            
            logger.info(f"[{self._session_id}] 🎯 ElevenLabs precise highlight: '{trigger_word}' → {highlight['category']} at {timestamp_ms:.1f}ms")
            
        except Exception as e:
            logger.error(f"[{self._session_id}] Error emitting precise highlight: {e}")
    
    def _get_latest_chart_context(self) -> Dict[str, Any]:
        """Get the most recent chart context for validation."""
        try:
            from app.tools.providers.system.chart_tools import get_latest_chart_context
            return get_latest_chart_context(self._session_id) or {}
        except Exception as e:
            logger.error(f"[{self._session_id}] Error getting chart context: {e}")
            return {}

