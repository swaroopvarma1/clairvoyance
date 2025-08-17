import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Mapping, Optional, List, Dict

from app.core.logger import logger
from pipecat.utils.text.base_text_filter import BaseTextFilter


class HighlightTextFilter(BaseTextFilter):
    """
    TTS text filter that extracts highlight XML tags and correlates with ElevenLabs word timing.
    
    Processes text like: "Credit cards <highlight category='Credit Card'>are crushing it</highlight> at 78%"
    - Extracts highlight information and stores for word timing correlation
    - Returns clean text: "Credit cards are crushing it at 78%" for TTS synthesis  
    - Correlates with TTSTextFrame from ElevenLabs for precise word timing
    - Emits highlights exactly when trigger words are spoken (e.g., "are" triggers highlight)
    """
    
    def __init__(self, session_id: str):
        """
        Initialize highlight text filter.
        
        Args:
            session_id: Session identifier for logging and context
        """
        super().__init__()
        self._session_id = session_id
        
        # XML pattern to match highlight tags
        self._highlight_pattern = re.compile(
            r'<highlight\s+category=["\']([^"\']+)["\'][^>]*>(.*?)</highlight>',
            re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"[{session_id}] 🎯 HighlightTextFilter initialized")
    
    async def update_settings(self, settings: Mapping[str, Any]):
        """Update filter settings (no settings needed for highlight filter)."""
        pass
    
    async def reset_interruption(self):
        """Reset filter state on interruption."""
        pass
    
    async def handle_interruption(self):
        """Handle filter interruption (required by BaseTextFilter)."""
        pass
    
    
    async def filter(self, text: str) -> str:
        """
        Extract highlights from text and emit RTVI events, return clean text for TTS.
        
        Args:
            text: Input text with potential highlight XML tags
            
        Returns:
            Clean text without XML tags for TTS synthesis
        """
        try:
            # Find all highlight matches
            logger.info(f"[{self._session_id}] 🔍 TTS Filter received text: '{text}'")
            highlights = self._extract_highlights(text)
            
            if highlights:
                logger.info(f"[{self._session_id}] 🎯 Found {len(highlights)} highlights in TTS filter")
                
                # Store highlights for later TTSTextFrame correlation
                from app.utils.highlight_storage import store_highlights_for_session
                store_highlights_for_session(self._session_id, highlights)
                logger.info(f"[{self._session_id}] 📦 Stored highlights for word timing: {[h['triggerWord'] for h in highlights]}")
            
            # Return clean text without XML tags
            clean_text = self._remove_highlight_tags(text)
            
            if highlights:
                logger.info(f"[{self._session_id}] 📢 TTS will speak: '{clean_text}'")
                logger.info(f"[{self._session_id}] ✨ Highlights emitted: {[h['category'] for h in highlights]}")
            
            return clean_text
            
        except Exception as e:
            logger.error(f"[{self._session_id}] Error in highlight filter: {e}")
            # Return original text on error to avoid breaking TTS
            return text
    
    def _extract_highlights(self, text: str) -> List[Dict[str, Any]]:
        """Extract highlight information from XML tags in text."""
        highlights = []
        
        for match in self._highlight_pattern.finditer(text):
            category = match.group(1).strip()
            spoken_text = match.group(2).strip()
            
            # Get chart context for validation
            chart_context = self._get_latest_chart_context()
            
            if chart_context:
                categories = chart_context.get('categories', [])
                if category in categories:
                    category_index = categories.index(category)
                    chart_id = chart_context.get('chartId', 'unknown')
                    
                    highlight_data = {
                        'category': category,
                        'spokenText': spoken_text,
                        'categoryIndex': category_index,
                        'chartId': chart_id,
                        'created_at': int(time.time() * 1000),
                        'timestamp': None,  # Will be set when first word is spoken
                        'triggerWord': spoken_text.split()[0] if spoken_text.split() else spoken_text
                    }
                    
                    highlights.append(highlight_data)
                    logger.debug(f"[{self._session_id}] Extracted highlight: {category} -> '{spoken_text}'")
                else:
                    # Fallback: Trust LLM's category choice even if not in stored context
                    # This handles timing issues where chart context hasn't updated yet
                    logger.warning(f"[{self._session_id}] Category '{category}' not in stored context {categories}, but trusting LLM choice")
                    
                    highlight_data = {
                        'category': category,
                        'spokenText': spoken_text,
                        'categoryIndex': -1,  # Frontend can find index
                        'chartId': chart_context.get('chartId', 'latest'),
                        'created_at': int(time.time() * 1000),
                        'timestamp': None,  # Will be set when first word is spoken
                        'triggerWord': spoken_text.split()[0] if spoken_text.split() else spoken_text
                    }
                    
                    highlights.append(highlight_data)
                    logger.info(f"[{self._session_id}] Fallback highlight created: {category} -> '{spoken_text}'")
            else:
                # No chart context - create highlight anyway, let frontend handle it
                logger.warning(f"[{self._session_id}] No chart context, creating fallback highlight for '{category}'")
                
                highlight_data = {
                    'category': category,
                    'spokenText': spoken_text,
                    'categoryIndex': -1,  # Frontend will resolve
                    'chartId': 'latest',
                    'created_at': int(time.time() * 1000),
                    'timestamp': None,  # Will be set when first word is spoken
                    'triggerWord': spoken_text.split()[0] if spoken_text.split() else spoken_text
                }
                
                highlights.append(highlight_data)
                logger.info(f"[{self._session_id}] No-context highlight created: {category} -> '{spoken_text}'")
        
        return highlights
    
    def _remove_highlight_tags(self, text: str) -> str:
        """Remove all highlight XML tags from text, keeping only the inner content."""
        # Replace highlight tags with just their inner content
        clean_text = self._highlight_pattern.sub(r'\2', text)
        return clean_text.strip()
    

    def _get_latest_chart_context(self) -> Optional[Dict[str, Any]]:
        """Get the most recent chart context for validation."""
        try:
            from app.tools.providers.system.chart_tools import get_latest_chart_context
            return get_latest_chart_context(self._session_id)
        except Exception as e:
            logger.error(f"[{self._session_id}] Error getting chart context: {e}")
            return None