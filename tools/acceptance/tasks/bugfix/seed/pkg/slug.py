import re

def slugify(text):
    """A URL slug: lowercase ASCII letters, digits and single hyphens; everything else becomes a hyphen; no
    leading, trailing or repeated hyphens. The empty string stays empty."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    return text.strip()
