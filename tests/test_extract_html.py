import pytest
from openagentsearch.extract.html import extract


def test_extract_basic():
    """Test basic HTML extraction with title, lang and visible content."""
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>Sample Page Title</title>
    </head>
    <body>
        <nav>SITE-NAV-BOILERPLATE-42</nav>
        <h1>Main Heading</h1>
        <p>This is some <b>visible</b> content.</p>
        <script>alert('hidden');</script>
        <style>body { color: red; }</style>
    </body>
    </html>
    """
    
    result = extract(html)
    assert result['text'] == "Main Heading This is some visible content."
    assert result['title'] == "Sample Page Title"
    assert result['lang'] == "en"


def test_extract_without_title_or_lang():
    """Test extraction when title and lang are absent."""
    html = """
    <!DOCTYPE html>
    <html>
    <body>
        <p>Some content here.</p>
        <nav>Navigation</nav>
        <script>console.log('test');</script>
        <style>.hidden { display: none; }</style>
    </body>
    </html>
    """
    
    result = extract(html)
    assert result['text'] == "Some content here."
    assert result['title'] == ""
    assert result['lang'] == ""


def test_extract_with_messy_whitespace():
    """Test extraction with inconsistent whitespace."""
    html = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <title>Page Titre</title>
    </head>
    <body>
        <p>  Multiple    spaces   and
            newlines   </p>
        <nav>BOILERPLATE</nav>
        <script>var x = 1;</script>
    </body>
    </html>
    """
    
    result = extract(html)
    assert result['text'] == "Multiple spaces and newlines"
    assert result['title'] == "Page Titre"
    assert result['lang'] == "fr"


def test_extract_excludes_script_style_nav():
    """Test that script, style, and nav content is excluded."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Exclusion Test</title>
    </head>
    <body>
        <nav>This should be excluded</nav>
        <p>Visible text here.</p>
        <script>var a = 'this should'; var b = 'be excluded';</script>
        <style>.hidden { display: none; } /* this too */</style>
        <h1>Main content</h1>
    </body>
    </html>
    """
    
    result = extract(html)
    # Verify that script/style/nav content is not in the final text
    assert "excluded" not in result['text']
    assert "hidden" not in result['text']
    assert "should be excluded" not in result['text']
    assert "Main content" in result['text']
    assert "Visible text here" in result['text']
    assert result['title'] == "Exclusion Test"
    assert result['lang'] == ""


def test_extract_empty_html():
    """Test extraction with minimal HTML."""
    html = "<html><body></body></html>"
    
    result = extract(html)
    assert result['text'] == ""
    assert result['title'] == ""
    assert result['lang'] == ""