import html.parser
import re
from typing import Dict, List


class HTMLExtractor(html.parser.HTMLParser):
    """Custom HTML parser to extract text content while excluding script, style, and nav elements."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: List[str] = []
        self.title: str = ""
        self.lang: str = ""
        self.in_title: bool = False
        self.in_script: bool = False
        self.in_style: bool = False
        self.in_nav: bool = False
        self.title_found: bool = False

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        """Handle opening tags."""
        if tag == "title":
            self.in_title = True
        elif tag == "script":
            self.in_script = True
        elif tag == "style":
            self.in_style = True
        elif tag == "nav":
            self.in_nav = True
        elif tag == "html" and not self.title_found:
            # Extract language attribute from html tag
            for attr_name, attr_value in attrs:
                if attr_name == "lang":
                    self.lang = attr_value
                    break

    def handle_endtag(self, tag: str) -> None:
        """Handle closing tags."""
        if tag == "title":
            self.in_title = False
        elif tag == "script":
            self.in_script = False
        elif tag == "style":
            self.in_style = False
        elif tag == "nav":
            self.in_nav = False

    def handle_data(self, data: str) -> None:
        """Handle text data."""
        # Only collect text when not inside script, style or nav elements
        if not (self.in_script or self.in_style or self.in_nav):
            if self.in_title:
                self.title += data
            else:
                self.text_parts.append(data)

    def get_text(self) -> str:
        """Get the combined and normalized text content."""
        # Join with spaces, then normalize multiple spaces to single space, strip
        return " ".join(self.text_parts).strip()


def extract(html: str) -> Dict[str, str]:
    """
    Extract structured metadata from an HTML document.

    Args:
        html (str): The HTML content as a string

    Returns:
        dict: A dictionary with keys 'text', 'title', and 'lang'
              - text: The visible text content (excluding script/style/nav)
              - title: The title element content or "" if not found
              - lang: The root html.lang attribute or "" if not found
    """
    parser = HTMLExtractor()
    parser.feed(html)
    
    # Normalize whitespace in text content to single spaces and strip
    normalized_text = re.sub(r'\s+', ' ', " ".join(parser.text_parts).strip())
    
    return {
        'text': normalized_text,
        'title': parser.title.strip(),
        'lang': parser.lang
    }