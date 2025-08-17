"""
Real-time XML highlight tag parser for voice-synchronized chart highlighting.
"""
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from app.core.logger import logger


class HighlightTagParser:
    """Parser for extracting highlight tags from streaming LLM text."""
    
    def __init__(self):
        # Regex pattern to match highlight tags
        self.highlight_pattern = re.compile(
            r"<highlight\s+category=['\"]([^'\"]+)['\"]>([^<]*)</highlight>",
            re.IGNORECASE
        )
        
    def parse_highlight_tags(self, text: str, chart_context: Optional[Dict[str, Any]] = None) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Parse highlight tags from text and return clean text + highlight data.
        
        Args:
            text: Text containing highlight tags
            chart_context: Chart context for category mapping
            
        Returns:
            Tuple of (cleaned_text, highlights_list)
        """
        highlights = []
        
        if not chart_context or not chart_context.get('categories'):
            # No chart context, just remove tags and return clean text
            clean_text = self.highlight_pattern.sub(r'\2', text)
            return clean_text, highlights
        
        categories = chart_context.get('categories', [])
        chart_id = chart_context.get('chartId', 'unknown')
        
        # Create category mapping for quick lookup
        category_mapping = {}
        for i, category in enumerate(categories):
            category_mapping[category] = i
            # Also add lowercase version for flexible matching
            category_mapping[category.lower()] = i
            
        def highlight_replacer(match):
            category_name = match.group(1)
            spoken_text = match.group(2)
            
            # Try exact match first, then case-insensitive
            category_index = category_mapping.get(category_name)
            if category_index is None:
                category_index = category_mapping.get(category_name.lower())
            
            if category_index is not None:
                # Valid category found in chart
                highlight = {
                    'chartId': chart_id,
                    'category': category_name,
                    'categoryIndex': category_index,
                    'spokenText': spoken_text,
                    'timestamp': int(time.time() * 1000),
                    'type': 'voice-highlight'
                }
                highlights.append(highlight)
                logger.debug(f"Parsed highlight tag: {category_name} -> index {category_index}")
            else:
                logger.warning(f"Highlight tag references unknown category: {category_name} (available: {categories})")
            
            # Return just the spoken text (remove XML tags)
            return spoken_text
        
        # Replace all highlight tags with spoken text and collect highlights
        clean_text = self.highlight_pattern.sub(highlight_replacer, text)
        
        if highlights:
            logger.info(f"Parsed {len(highlights)} highlight tags from text")
        
        return clean_text, highlights
    
    def extract_partial_highlights(self, text_chunk: str, accumulated_text: str, chart_context: Optional[Dict[str, Any]] = None) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Extract highlights from partial text chunks for real-time streaming.
        
        Args:
            text_chunk: New text chunk from LLM
            accumulated_text: Previously accumulated text
            chart_context: Chart context for category mapping
            
        Returns:
            Tuple of (clean_chunk, new_highlights)
        """
        # Check if the new chunk might complete a highlight tag
        combined_text = accumulated_text + text_chunk
        
        # Find all complete highlight tags in the combined text
        clean_combined, all_highlights = self.parse_highlight_tags(combined_text, chart_context)
        
        # Find previously extracted highlights to avoid duplicates
        clean_accumulated, prev_highlights = self.parse_highlight_tags(accumulated_text, chart_context)
        
        # New highlights are those not in previous extraction
        new_highlights = []
        prev_categories = set((h['category'], h['categoryIndex']) for h in prev_highlights)
        
        for highlight in all_highlights:
            key = (highlight['category'], highlight['categoryIndex'])
            if key not in prev_categories:
                new_highlights.append(highlight)
        
        # Clean chunk is the difference between clean combined and clean accumulated
        clean_chunk = clean_combined[len(clean_accumulated):]
        
        return clean_chunk, new_highlights
    
    def is_highlight_tag_complete(self, text: str) -> bool:
        """
        Check if text contains complete highlight tags (for streaming detection).
        
        Args:
            text: Text to check
            
        Returns:
            True if text contains complete highlight tags
        """
        return bool(self.highlight_pattern.search(text))
    
    def has_partial_highlight_tag(self, text: str) -> bool:
        """
        Check if text might contain partial highlight tags (for buffering).
        
        Args:
            text: Text to check
            
        Returns:
            True if text might contain incomplete highlight tags
        """
        # Check for opening tag without closing
        partial_patterns = [
            r'<highlight\s+category=[\'"][^\'\"]*[\'"]>[^<]*$',  # Started tag, no closing
            r'<highlight\s+category=[\'"][^\'\"]*$',  # Incomplete opening tag
            r'<highlight\s*$',  # Just opening bracket
        ]
        
        for pattern in partial_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
                
        return False