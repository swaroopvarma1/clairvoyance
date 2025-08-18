# Frontend Integration Guide: Real-Time VAD Parameter Updates

## Overview

This guide explains how to implement a debug panel in the frontend that can update Voice Activity Detection (VAD) parameters in real-time during an active voice session with Clairvoyance.

## Technical Architecture

The communication uses the RTVI (Real-Time Voice Interface) protocol over WebSocket connections established through Daily.co rooms. The voice agent listens for custom client messages and can update VAD parameters without restarting the pipeline.

## Message Protocol

### Sending VAD Parameter Updates

**Message Format:**
```javascript
{
  "action": "update_vad_params",
  "data": {
    "confidence": 0.85,     // Optional: 0.0 - 1.0
    "start_secs": 0.30,     // Optional: 0.0 - 5.0 seconds
    "stop_secs": 1.00,      // Optional: 0.1 - 10.0 seconds
    "min_volume": 0.75      // Optional: 0.0 - 1.0
  }
}
```

### Response Messages

**Success Response:**
```javascript
{
  "type": "vad_params_updated",
  "data": {
    "updated_params": {
      "confidence": 0.85,
      "start_secs": 0.30
    },
    "current_params": {
      "confidence": 0.85,
      "start_secs": 0.30,
      "stop_secs": 1.00,
      "min_volume": 0.75
    }
  }
}
```

**Error Response:**
```javascript
{
  "type": "error",
  "message": "Confidence must be between 0.0 and 1.0"
}
```

## Implementation Example

### 1. Basic RTVI Message Sending

```javascript
// Assuming you have access to the Daily call object
function sendVADUpdate(vadParams) {
  const message = {
    action: "update_vad_params",
    data: vadParams
  };
  
  // Send via Daily's sendAppMessage for RTVI protocol
  daily.sendAppMessage(message, "*");
}

// Example usage
sendVADUpdate({
  confidence: 0.9,
  start_secs: 0.25
});
```

### 2. Complete Debug Panel Implementation

```html
<!-- VAD Debug Panel -->
<div id="vad-debug-panel">
  <h3>VAD Parameters Debug Panel</h3>
  
  <div class="param-control">
    <label for="confidence">Confidence (0.0 - 1.0):</label>
    <input type="range" id="confidence" min="0" max="1" step="0.01" value="0.85">
    <span id="confidence-value">0.85</span>
  </div>
  
  <div class="param-control">
    <label for="start-secs">Start Seconds (0.0 - 5.0):</label>
    <input type="range" id="start-secs" min="0" max="5" step="0.1" value="0.3">
    <span id="start-secs-value">0.3</span>
  </div>
  
  <div class="param-control">
    <label for="stop-secs">Stop Seconds (0.1 - 10.0):</label>
    <input type="range" id="stop-secs" min="0.1" max="10" step="0.1" value="1.0">
    <span id="stop-secs-value">1.0</span>
  </div>
  
  <div class="param-control">
    <label for="min-volume">Min Volume (0.0 - 1.0):</label>
    <input type="range" id="min-volume" min="0" max="1" step="0.01" value="0.75">
    <span id="min-volume-value">0.75</span>
  </div>
  
  <button id="update-vad">Update VAD Parameters</button>
  <div id="vad-status"></div>
</div>
```

### 3. JavaScript Event Handlers

```javascript
class VADDebugPanel {
  constructor(dailyCall) {
    this.daily = dailyCall;
    this.initializeEventListeners();
    this.setupMessageHandlers();
  }
  
  initializeEventListeners() {
    // Update display values when sliders change
    ['confidence', 'start-secs', 'stop-secs', 'min-volume'].forEach(param => {
      const slider = document.getElementById(param);
      const display = document.getElementById(`${param}-value`);
      
      slider.addEventListener('input', (e) => {
        display.textContent = e.target.value;
      });
    });
    
    // Send update when button clicked
    document.getElementById('update-vad').addEventListener('click', () => {
      this.sendVADUpdate();
    });
  }
  
  sendVADUpdate() {
    const params = {
      confidence: parseFloat(document.getElementById('confidence').value),
      start_secs: parseFloat(document.getElementById('start-secs').value),
      stop_secs: parseFloat(document.getElementById('stop-secs').value),
      min_volume: parseFloat(document.getElementById('min-volume').value)
    };
    
    const message = {
      action: "update_vad_params",
      data: params
    };
    
    this.daily.sendAppMessage(message, "*");
    this.updateStatus("Sending VAD update...", "info");
  }
  
  setupMessageHandlers() {
    this.daily.on('app-message', (event) => {
      const message = event.data;
      
      if (message.type === 'vad_params_updated') {
        this.handleVADUpdateSuccess(message.data);
      } else if (message.type === 'error') {
        this.handleVADUpdateError(message.message);
      }
    });
  }
  
  handleVADUpdateSuccess(data) {
    this.updateStatus(
      `VAD parameters updated successfully: ${JSON.stringify(data.updated_params)}`, 
      "success"
    );
    
    // Optionally update UI to show current values
    this.updateUIWithCurrentParams(data.current_params);
  }
  
  handleVADUpdateError(errorMessage) {
    this.updateStatus(`Error: ${errorMessage}`, "error");
  }
  
  updateUIWithCurrentParams(params) {
    // Update sliders and displays with actual values from voice agent
    Object.entries(params).forEach(([key, value]) => {
      const elementId = key.replace('_', '-');
      const slider = document.getElementById(elementId);
      const display = document.getElementById(`${elementId}-value`);
      
      if (slider && display) {
        slider.value = value;
        display.textContent = value;
      }
    });
  }
  
  updateStatus(message, type) {
    const statusDiv = document.getElementById('vad-status');
    statusDiv.textContent = message;
    statusDiv.className = `status ${type}`;
    
    // Clear status after 5 seconds
    setTimeout(() => {
      statusDiv.textContent = '';
      statusDiv.className = 'status';
    }, 5000);
  }
}

// Initialize when Daily call is ready
const vadDebugPanel = new VADDebugPanel(daily);
```

### 4. CSS Styling (Optional)

```css
#vad-debug-panel {
  background: #f5f5f5;
  padding: 20px;
  border-radius: 8px;
  margin: 20px;
  max-width: 400px;
}

.param-control {
  margin: 15px 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.param-control label {
  min-width: 150px;
  font-weight: bold;
}

.param-control input[type="range"] {
  flex: 1;
}

.param-control span {
  min-width: 40px;
  font-family: monospace;
}

#update-vad {
  background: #007bff;
  color: white;
  border: none;
  padding: 10px 20px;
  border-radius: 4px;
  cursor: pointer;
  margin-top: 15px;
}

#update-vad:hover {
  background: #0056b3;
}

.status {
  margin-top: 10px;
  padding: 8px;
  border-radius: 4px;
  font-size: 14px;
}

.status.success {
  background: #d4edda;
  color: #155724;
  border: 1px solid #c3e6cb;
}

.status.error {
  background: #f8d7da;
  color: #721c24;
  border: 1px solid #f5c6cb;
}

.status.info {
  background: #d1ecf1;
  color: #0c5460;
  border: 1px solid #bee5eb;
}
```

## Parameter Descriptions

### VAD Parameters Explained

- **confidence** (0.0 - 1.0): Sensitivity threshold for voice detection. Higher values require clearer speech.
- **start_secs** (0.0 - 5.0): Delay before considering speech has started (helps filter out noise).
- **stop_secs** (0.1 - 10.0): Duration of silence before considering speech has stopped.
- **min_volume** (0.0 - 1.0): Minimum audio volume threshold for voice detection.

### Recommended Values

- **Low Noise Environment**: confidence=0.9, start_secs=0.2, stop_secs=0.8, min_volume=0.6
- **High Noise Environment**: confidence=0.7, start_secs=0.4, stop_secs=1.2, min_volume=0.8
- **Sensitive Detection**: confidence=0.6, start_secs=0.1, stop_secs=0.5, min_volume=0.5

## Integration Checklist

1. ✅ Ensure Daily.co WebSocket connection is established
2. ✅ Implement RTVI message sending via `daily.sendAppMessage()`
3. ✅ Set up message event listeners for responses
4. ✅ Add parameter validation on frontend (optional but recommended)
5. ✅ Implement user feedback for successful/failed updates
6. ✅ Test with different parameter combinations
7. ✅ Add error handling for edge cases

## Troubleshooting

### Common Issues

1. **No Response from Voice Agent**
   - Verify WebSocket connection is active
   - Check that voice agent session is running
   - Ensure message format matches exactly

2. **Parameter Update Rejected**
   - Check parameter values are within valid ranges
   - Verify all required fields are included
   - Check browser console for error messages

3. **UI Not Updating**
   - Ensure event listeners are properly attached
   - Check for JavaScript errors in console
   - Verify Daily.co event handlers are working

### Debug Tips

```javascript
// Enable debug logging
daily.on('app-message', (event) => {
  console.log('Received message:', event.data);
});

// Test message sending
daily.sendAppMessage({
  action: "update_vad_params",
  data: { confidence: 0.8 }
}, "*");
```

## Notes

- Parameter updates take effect immediately without restarting the voice pipeline
- All parameters are optional - you can update just one at a time
- The system validates all parameters and provides detailed error messages
- Current parameter values are returned with each successful update
- Changes persist for the duration of the voice session only

## Support

For questions or issues with the VAD debug panel implementation, check the voice agent logs for detailed error messages and validation feedback.