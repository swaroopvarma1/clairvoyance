"""
TTS utilities for word-level highlighting with timestamps.
"""
import re
from typing import Dict, Any, List, Optional
from app.core.logger import logger


def add_ssml_marks_for_highlights(text: str, chart_context: Optional[Dict[str, Any]] = None) -> str:
    """
    Add SSML <mark> tags around words that should trigger chart highlights.
    
    Args:
        text: The text to be spoken by TTS
        chart_context: Chart context containing categories that can be highlighted
        
    Returns:
        SSML-formatted text with <mark> tags around highlight-worthy words
    """
    if not chart_context or not chart_context.get('categories'):
        return text
    
    categories = chart_context.get('categories', [])
    chart_id = chart_context.get('chartId', 'unknown')
    
    # Convert text to SSML format
    ssml_text = f'<speak>{text}</speak>'
    
    # Add marks around category words (case-insensitive matching)
    for i, category in enumerate(categories):
        if isinstance(category, str) and len(category.strip()) > 0:
            # Create a regex pattern for case-insensitive word boundary matching
            pattern = r'\b' + re.escape(category.strip()) + r'\b'
            
            # Create mark with category index for highlighting
            mark_tag = f'<mark name="highlight_{chart_id}_{i}"/>'
            replacement = f'{mark_tag}{category.strip()}'
            
            # Replace first occurrence only to avoid duplicate marks
            ssml_text = re.sub(pattern, replacement, ssml_text, count=1, flags=re.IGNORECASE)
    
    logger.debug(f"Added SSML marks for chart {chart_id}: {len(categories)} categories")
    return ssml_text


# TTS timestamp extraction removed - not available in current Pipecat version
# This functionality will be added when TTS timestamp frames become available


def is_ssml_supported_text(text: str) -> bool:
    """
    Check if text should be converted to SSML for timestamp support.
    
    Args:
        text: Text to check
        
    Returns:
        True if text should use SSML marks for highlighting
    """
    # Use SSML for text that likely contains chart references
    chart_keywords = [
        'chart', 'graph', 'data', 'category', 'percentage', 'sales', 
        'breakdown', 'distribution', 'performance', 'metrics'
    ]
    
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in chart_keywords)