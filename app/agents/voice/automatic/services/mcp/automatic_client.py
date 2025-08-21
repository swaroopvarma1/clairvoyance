import httpx
import json
import base64
import uuid
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, Callable

from app.core.logger import logger
from app.core import config

def _generate_success_message(function_name: str, arguments: Dict[str, Any]) -> str:
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
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.schemas.function_schema import FunctionSchema
from app.agents.voice.automatic.types.models import (
    JSONRPCResponse,
    ToolCallResult,
    MCPTool
)

class StreamableHTTPTransport:
    """Handles JSON-RPC 2.0 over streaming HTTP with custom headers."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        logger.debug(f"StreamableHTTPTransport initialized with server_url: '{server_url}'")
        if not server_url or not isinstance(server_url, str):
            raise ValueError("MCP server URL must be a non-empty string.")

        self._server_url = server_url.strip()
        self._auth_token = auth_token
        self._context_b64 = base64.b64encode(json.dumps(context).encode()).decode()
        self._client = httpx.AsyncClient(timeout=15)
        self._demo_mode = context.get("enableDemoMode", False)

    async def post(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Performs a JSON-RPC POST request and handles streaming response."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-context": self._context_b64,
        }
        if self._auth_token:
            headers["x-auth-token"] = self._auth_token
        json_rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        
        query_params = {}
        if self._demo_mode:
            query_params["demoMode"] = "true"

        try:
            logger.info(f"Attempting to POST to: {self._server_url} with payload: {json_rpc_payload} and headers: {headers}")
            async with self._client.stream("POST", self._server_url, headers=headers, json=json_rpc_payload, params=query_params) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        json_str = line[len("data:"):].strip()
                        try:
                            validated_response = JSONRPCResponse.model_validate_json(json_str)
                            response_dict = validated_response.model_dump(by_alias=True, exclude_none=True)

                            if isinstance(validated_response.result, ToolCallResult):
                                for i, item in enumerate(validated_response.result.content):
                                    response_dict["result"]["content"][i]["text"] = item.text

                            return response_dict
                        except json.JSONDecodeError:
                            logger.error(f"Failed to decode JSON from stream: {json_str}")
                            raise ValueError("Received malformed JSON from server.")
                        except Exception as e: # Catches Pydantic's ValidationError
                            logger.error(f"Response validation failed: {e}")
                            raise ValueError(f"Server response did not match expected schema: {e}")

                raise ValueError("Server stream ended without sending a data message.")

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error on method {method}: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"HTTP Error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Network request error on method {method}: {e}")
            raise RuntimeError(f"Network Error: {e}")
        except Exception as e:
            logger.error(f"An unexpected transport error occurred on method {method}: {e}")
            raise

    async def _handle_confirmation_required(self, function_name: str, tool_call_id: str, arguments: Dict[str, Any], result_callback: Callable):
        """Handle MCP operations that require user confirmation"""
        
        confirmation_id = str(uuid.uuid4())
        
        try:
            # Send SSE to Lighthouse and wait for response
            response = await self._request_user_confirmation(confirmation_id, function_name, arguments)
            
            # Check if user approved the operation
            approved = response.get('approved', False) if response else False
            
            if approved:
                # Execute with original or modified parameters
                final_args = response.get('modified_arguments', arguments)
                logger.info(f"User approved MCP function {function_name}, executing...")
                
                # Need to call the tool through the parent MCPClient
                params = {"name": function_name, "arguments": final_args}
                response_dict = await self.post(method="tools/call", params=params)
                
                if response_dict.get("error"):
                    error_details = response_dict['error']
                    logger.error(f"MCP tool execution failed with error: {error_details}")
                    await result_callback(f"Error: {error_details}")
                    return
                
                result = response_dict.get("result", {})
                
                text_responses = []
                content_items = result.get("content", [])
                
                for i, item in enumerate(content_items):
                    if item.get("type") == "text" and item.get("text"):
                        text_responses.append(str(item.get("text")))
                
                text_response = " ".join(text_responses) if text_responses else "Tool executed successfully."
                
                # Generate and append success message
                success_msg = _generate_success_message(function_name, final_args)
                enhanced_response = f"{text_response}\n\n{success_msg}"
                
                await result_callback(enhanced_response)
                
            else:
                # User rejected, timed out, or error occurred
                reason = response.get('reason', 'unknown') if response else 'no response'
                logger.info(f"MCP function {function_name} not approved. Reason: {reason}")
                
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
            logger.error(f"Confirmation process failed for MCP function {function_name}: {e}")
            await result_callback(f"Confirmation failed: {str(e)}")

    async def _request_user_confirmation(self, confirmation_id: str, function_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
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
            logger.warning(f"Timeout waiting for confirmation of MCP function {function_name}")
            return {"action": "timeout"}
        except Exception as e:
            logger.error(f"Error in MCP confirmation process: {e}")
            return {"action": "error", "error": str(e)}

    async def _send_confirmation_to_rtvi(self, payload: Dict[str, Any]):
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

    async def _wait_for_user_response(self, confirmation_id: str, timeout_seconds: int) -> Dict[str, Any]:
        """Wait for user response via RTVI"""
        
        # Register this confirmation as pending and wait for RTVI response
        from app.agents.voice.automatic.processors.llm_spy import register_pending_confirmation, wait_for_confirmation_response
        
        try:
            # Register the pending confirmation
            register_pending_confirmation(confirmation_id)
            
            # Wait for RTVI response with timeout
            response = await wait_for_confirmation_response(confirmation_id, timeout_seconds)
            
            logger.info(f"Received user response via RTVI for MCP function {confirmation_id}: approved={response.get('approved', False) if response else False}")
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

    async def close(self):
        await self._client.aclose()

class MCPClient:
    """A service to list, register, and call tools from a remote MCP server."""
    def __init__(self, server_url: str, auth_token: str, context: Dict[str, Any]):
        self._transport = StreamableHTTPTransport(server_url, auth_token, context)
        self._llm = None

    async def register_tools(self, llm, selective_functions) -> ToolsSchema:
        """Lists tools and registers them with the given LLM processor."""
        self._llm = llm
        logger.info("Registering tools from custom MCP client...")
        selective_functions_set = set(selective_functions)
        try:
            response_dict = await self._transport.post(method="tools/list")
            
            if response_dict.get("error"):
                error_details = response_dict['error']
                logger.error(f"Received JSON-RPC error when listing tools: {error_details}")
                raise RuntimeError(f"JSON-RPC Error listing tools: {error_details}")

            if not response_dict.get("result") or not response_dict["result"].get("tools"):
                logger.warning("Tool registration response was successful but contained no tools.")
                return ToolsSchema(standard_tools=[])

            raw_tools = response_dict["result"]["tools"]
            
            selective_tools_to_register = []
            if len(selective_functions) > 0:
                for tool_data in raw_tools:
                    tool_name = tool_data["name"]
                    if tool_name in selective_functions_set:
                        selective_tools_to_register.append(tool_data)
                        
            tools_to_process = raw_tools
            if len(selective_tools_to_register) > 0:
                tools_to_process = selective_tools_to_register
            
            converted_tools = []
            for tool_data in tools_to_process:
                tool_name = tool_data["name"]
                logger.debug(f"Registering remote tool: {tool_name}")
                
                function_schema = self._convert_schema(tool_data)
                converted_tools.append(function_schema)
                
                llm.register_function(tool_name, self._mcp_tool_wrapper)
                
            logger.info(f"Successfully registered {len(converted_tools)} remote tools.")
            return ToolsSchema(standard_tools=converted_tools)
        except Exception as e:
            logger.error(f"Failed to register tools from remote server: {e}")
            return ToolsSchema(standard_tools=[])

    def _convert_schema(self, tool_data: Dict[str, Any]) -> FunctionSchema:
        """Converts a raw MCP tool dict to a PipeCat FunctionSchema."""
        tool = MCPTool.model_validate(tool_data)
        return FunctionSchema(
            name=tool.name,
            description=tool.description,
            properties=tool.input_schema.properties,
            required=tool.input_schema.required or [],
        )

    async def _mcp_tool_wrapper(
        self, function_name: str, tool_call_id: str, arguments: Dict[str, Any],
        llm: Any, context: Any, result_callback: Callable
    ) -> None:
        """This wrapper is called by the LLM. It then calls the remote tool."""
        
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
        
        logger.debug(f"LLM called tool: {function_name} with args: {arguments}")
        
        # Check if function confirmation is enabled and this is a dangerous operation
        if config.ENABLE_FUNCTION_CONFIRMATION and is_write_operation:
            logger.info(f"Intercepted dangerous MCP function call: {function_name}")
            await self._transport._handle_confirmation_required(
                function_name, tool_call_id, arguments, result_callback
            )
        else:
            # Safe operation or confirmation disabled - execute immediately
            await self._call_tool(function_name, arguments, result_callback)

    async def _call_tool(
        self, function_name: str, arguments: Dict[str, Any], result_callback: Callable
    ) -> None:
        """Sends the 'tools/call' request to the remote server."""
        try:
            params = {"name": function_name, "arguments": arguments}
            logger.debug(f"Calling MCP tool {function_name} with params: {params}")
            
            response_dict = await self._transport.post(method="tools/call", params=params)

            if response_dict.get("error"):
                error_details = response_dict['error']
                logger.error(f"MCP tool execution failed with error: {error_details}")
                raise RuntimeError(f"JSON-RPC Error calling tool: {error_details}")

            result = response_dict.get("result", {})
            
            text_responses = []
            ui_components = []

            # Parse MCP response structure: result.content[0].text
            content_items = result.get("content", [])
            
            for i, item in enumerate(content_items):
                if item.get("type") == "text" and item.get("text"):
                    text_data = item.get("text")
                    if isinstance(text_data, dict) and text_data.get("uiComponent") is True:
                        ui_components.append(text_data)
                    else:
                        text_responses.append(str(text_data))

            # Store UI components if any
            if ui_components:
                await self._store_ui_components_from_mcp(ui_components)
            
            # Prepare text response for LLM
            text_response = " ".join(text_responses)
            if not text_response:
                text_response = "Tool executed successfully but returned no text."

            if ui_components:
                logger.info(f"Tool also returned {len(ui_components)} UI components")
                
            await result_callback(text_response)

        except Exception as e:
            logger.error(f"Failed to call tool '{function_name}': {e}")
            await result_callback(f"Error: Could not execute tool {function_name}.")

    async def close(self):
        await self._client.aclose()
