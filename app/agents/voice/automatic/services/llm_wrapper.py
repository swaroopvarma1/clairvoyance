import asyncio
import httpx
import uuid
import json
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
from app.agents.voice.automatic.services.context_summarizer import ContextSummarizer
from app.core import config
from app.core.logger import logger

def _generate_success_message(function_name: str, arguments: dict) -> str:
    """Generate a success confirmation message based on function name and arguments"""
    
    # Extract operation type from function name
    operation_type = None
    entity_name = None
    
    if function_name.startswith('create'):
        operation_type = "created"
        entity_name = function_name[6:].replace('_', ' ').strip()
    elif function_name.startswith('update'):
        operation_type = "updated"
        entity_name = function_name[6:].replace('_', ' ').strip()
    elif function_name.startswith('delete'):
        operation_type = "deleted"
        entity_name = function_name[6:].replace('_', ' ').strip()
    else:
        # Check arguments for action type
        action_value = arguments.get('action', '').lower() if isinstance(arguments, dict) else ''
        if action_value in ['create', 'update', 'delete', 'pause']:
            operation_type = action_value + "d" if action_value != 'pause' else "paused"
            entity_name = arguments.get('type', 'item').replace('_', ' ')
    
    # Generate appropriate success message
    if operation_type and entity_name:
        return f"✅ Operation completed successfully. The {entity_name} has been {operation_type} as requested."
    elif operation_type:
        return f"✅ Operation completed successfully. The item has been {operation_type} as requested."
    else:
        return "✅ Operation completed successfully."

class LLMServiceWrapper:
    def __init__(self, llm_service):
        self._llm_service = llm_service
        self._pending_confirmations = {}
        
        # Store original method and override it
        self._original_execute = getattr(llm_service, '_execute_function_call', None)
        if self._original_execute:
            llm_service._execute_function_call = self._wrapped_execute_function_call

    def create_summarizing_context(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ContextSummarizer:
        """Create a summarizing context with the given parameters"""
        context = ContextSummarizer(
            messages=messages,
            tools=tools,
            llm_service=self._llm_service,
            max_turns_before_summary=config.MAX_TURNS_BEFORE_SUMMARY,
            keep_recent_turns=config.KEEP_RECENT_TURNS,
            enable_summarization=config.ENABLE_SUMMARIZATION
        )
        return context

    async def _wrapped_execute_function_call(self, function_name: str, tool_call_id: str, arguments: dict, llm, context, result_callback: Callable):
        """Intercept function calls to add confirmation for dangerous operations"""
        
        # Check if this is a write operation by function name
        is_write_operation_by_name = function_name.startswith(('update', 'create', 'delete'))
        
        # Check if this is a write operation by function arguments
        is_write_operation_by_args = False
        if isinstance(arguments, dict):
            action_value = arguments.get('action', '').lower()
            is_write_operation_by_args = action_value in ['delete', 'create', 'update', 'pause']
        
        # Combined check
        is_write_operation = is_write_operation_by_name or is_write_operation_by_args
        
        if is_write_operation:
            if is_write_operation_by_name:
                logger.info("Function call: %s - This is a WRITE operation (by name)", function_name)
            if is_write_operation_by_args:
                action_value = arguments.get('action', '')
                logger.info("Function call: %s - This is a WRITE operation (by action: %s)", function_name, action_value)
        else:
            logger.debug("Function call: %s - This is NOT a write operation", function_name)
        
        # Check if function confirmation is enabled
        if not config.ENABLE_FUNCTION_CONFIRMATION:
            await self._original_execute(function_name, tool_call_id, arguments, llm, context, result_callback)
            return
        
        # Check if this is a dangerous operation that requires confirmation
        if is_write_operation:
            logger.info(f"Intercepted dangerous function call: {function_name}")
            await self._handle_confirmation_required(
                function_name, tool_call_id, arguments, llm, context, result_callback
            )
        else:
            # Safe operation - execute immediately
            logger.debug(f"Executing safe function: {function_name}")
            await self._original_execute(function_name, tool_call_id, arguments, llm, context, result_callback)

    async def _handle_confirmation_required(self, function_name: str, tool_call_id: str, arguments: dict, llm, context, result_callback: Callable):
        """Handle operations that require user confirmation"""
        
        confirmation_id = str(uuid.uuid4())
        
        # Store pending operation
        self._pending_confirmations[confirmation_id] = {
            'function_name': function_name,
            'tool_call_id': tool_call_id,
            'arguments': arguments,
            'llm': llm,
            'context': context,
            'result_callback': result_callback,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            # Send SSE to Lighthouse and wait for response
            response = await self._request_user_confirmation(confirmation_id, function_name, arguments)
            
            # Check if user approved the operation
            approved = response.get('approved', False) if response else False
            
            if approved:
                # Execute with original or modified parameters
                final_args = response.get('modified_arguments', arguments)
                logger.info(f"User approved function {function_name}, executing...")
                
                # Create a wrapper callback to add success message
                async def success_callback(result):
                    # Generate success message
                    success_msg = _generate_success_message(function_name, final_args)
                    
                    # Combine original result with success message
                    if isinstance(result, str):
                        enhanced_result = f"{result}\n\n{success_msg}"
                    else:
                        enhanced_result = f"{str(result)}\n\n{success_msg}"
                    
                    await result_callback(enhanced_result)
                
                await self._original_execute(function_name, tool_call_id, final_args, llm, context, success_callback)
                
            else:
                # User rejected, timed out, or error occurred
                reason = response.get('reason', 'unknown') if response else 'no response'
                logger.info(f"Function {function_name} not approved. Reason: {reason}")
                
                # Import custom exceptions
                from app.agents.voice.automatic.exceptions import (
                    UserRejectedOperationError, 
                    OperationTimeoutError, 
                    ConfirmationError
                )
                
                # Throw appropriate exception to prevent LLM retries
                if reason == 'timeout':
                    raise OperationTimeoutError(f"Operation '{function_name}' timed out waiting for user confirmation")
                elif 'reject' in reason.lower() or 'denied' in reason.lower():
                    raise UserRejectedOperationError(f"User rejected operation '{function_name}'")
                else:
                    raise ConfirmationError(f"Operation '{function_name}' failed during confirmation: {reason}")
                
        except Exception as e:
            logger.error(f"Confirmation process failed for {function_name}: {e}")
            await result_callback(f"Confirmation failed: {str(e)}")
        finally:
            # Clean up
            self._pending_confirmations.pop(confirmation_id, None)

    async def _request_user_confirmation(self, confirmation_id: str, function_name: str, arguments: dict) -> dict:
        """Send SSE to Lighthouse and wait for response"""
        
        # Generate user-friendly action description
        action_type = self._get_action_description(function_name)
        
        # Prepare SSE payload
        sse_payload = {
            "type": "function_confirmation_request",
            "confirmation_id": confirmation_id,
            "action_type": action_type,
            "function_name": function_name,
            "arguments": arguments,
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            # Send confirmation request via RTVI
            await self._send_confirmation_to_rtvi(sse_payload)
            
            # Wait for user response with timeout
            response = await self._wait_for_user_response(confirmation_id, config.FUNCTION_CONFIRMATION_TIMEOUT)
            
            return response
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for confirmation of {function_name}")
            return {"action": "timeout"}
        except Exception as e:
            logger.error(f"Error in confirmation process: {e}")
            return {"action": "error", "error": str(e)}

    async def _send_confirmation_to_rtvi(self, payload: dict):
        """Send function confirmation request via RTVI"""
        try:
            # Import here to avoid circular imports
            from app.agents.voice.automatic.processors.llm_spy import get_rtvi_processor
            
            rtvi = get_rtvi_processor()
            if rtvi:
                from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
                
                # Send confirmation request via RTVI
                await rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "function-confirmation-request",
                            "payload": {
                                "confirmationId": payload["confirmation_id"],
                                "actionType": payload["action_type"],
                                "functionName": payload["function_name"],
                                "arguments": payload["arguments"],
                                "timestamp": payload["timestamp"]
                            }
                        }
                    )
                )
                logger.info(f"Function confirmation request sent via RTVI: {payload['function_name']} with action {payload.get('arguments', {}).get('action', 'N/A')}")
            else:
                logger.warning("RTVI processor not available for function confirmation")
                
        except Exception as e:
            logger.error(f"Failed to send function confirmation via RTVI: {e}")
            raise

    async def _wait_for_user_response(self, confirmation_id: str, timeout_seconds: int) -> dict:
        """Wait for user response via RTVI"""
        
        # Register this confirmation as pending and wait for RTVI response
        from app.agents.voice.automatic.processors.llm_spy import register_pending_confirmation, wait_for_confirmation_response
        
        try:
            # Register the pending confirmation
            register_pending_confirmation(confirmation_id)
            
            # Wait for RTVI response with timeout
            response = await wait_for_confirmation_response(confirmation_id, timeout_seconds)
            
            logger.info(f"Received user response via RTVI for local function {confirmation_id}: approved={response.get('approved', False) if response else False}")
            return response
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for RTVI confirmation response: {confirmation_id}")
            raise
        except Exception as e:
            logger.error(f"Error waiting for RTVI confirmation response: {e}")
            raise

    def _get_action_description(self, function_name: str) -> str:
        """Convert function name to user-friendly action description"""
        
        # Simple mapping based on function name patterns
        if function_name.startswith('update'):
            return f"Update {self._extract_entity_name(function_name)}"
        elif function_name.startswith('create'):
            return f"Create {self._extract_entity_name(function_name)}"
        elif function_name.startswith('delete'):
            return f"Delete {self._extract_entity_name(function_name)}"
        else:
            return function_name.replace('_', ' ').title()

    def _extract_entity_name(self, function_name: str) -> str:
        """Extract entity name from function name"""
        
        # Remove common prefixes
        for prefix in ['update_', 'create_', 'delete_', 'update', 'create', 'delete']:
            if function_name.startswith(prefix):
                entity = function_name[len(prefix):]
                break
        else:
            entity = function_name
        
        # Convert to readable format
        return entity.replace('_', ' ').title()

    def __getattr__(self, name):
        return getattr(self._llm_service, name)
