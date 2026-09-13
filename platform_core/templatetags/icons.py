# ruff: noqa: E501 - SVG path data is one token; wrapping it only makes it unreadable.
"""One icon set, named rather than pasted.

Icons were previously written as full inline SVG at every call site, which made
a nav entry twenty lines long and meant the same glyph existed in several
slightly different forms. `{% icon "graph" %}` renders from the registry below.

Two rules the call sites depend on:

* An unknown name raises rather than rendering nothing. A silently missing icon
  is the kind of thing nobody notices until a screenshot; a template error is
  found by the first test that renders the page.
* The icon goes BEFORE its label inside a link, so `<a>...>Label</a>` still
  holds and the navigation assertions that pin those strings keep working.
"""

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

#: Stroked 24x24 paths, so every icon shares one visual weight.
ICONS = {
    # Navigation and structure
    "graph": '<path d="M4 5a2 2 0 0 1 2-2h13v18H6a2 2 0 0 1-2-2z"/><path d="M9 8h6"/><path d="M9 12h4"/>',
    "chat": '<path d="M20 14a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
    "code": '<path d="m9 8-4 4 4 4"/><path d="m15 8 4 4-4 4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.9 14.3a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.2a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7h-.2a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.1-2.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5v-.2a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.2a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "organization": '<path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M10 21v-5h4v5"/>',
    "application": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18"/>',
    "audit": '<path d="M14 3v5h5"/><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9l5 5v11a2 2 0 0 1-2 2z"/><path d="M9 13h6"/><path d="M9 17h4"/>',
    # Settings sections
    "sliders": '<path d="M4 6h11"/><path d="M19 6h1"/><circle cx="17" cy="6" r="2"/><path d="M4 18h1"/><path d="M9 18h11"/><circle cx="7" cy="18" r="2"/>',
    "cost": '<circle cx="12" cy="12" r="9"/><path d="M12 7v10"/><path d="M14.5 9.5A2.5 2.5 0 0 0 12 8c-1.4 0-2.5.9-2.5 2s1.1 2 2.5 2 2.5.9 2.5 2-1.1 2-2.5 2a2.5 2.5 0 0 1-2.5-1.5"/>',
    "plug": '<path d="M9 2v6"/><path d="M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
    "people": '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5.5a3 3 0 0 1 0 5"/><path d="M18 20a6 6 0 0 0-3-5"/>',
    "toggle": '<rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="16" cy="12" r="3"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4l3 2"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8-8"/><path d="m17 5 2.5 2.5"/><path d="m14.5 7.5 2.5 2.5"/>',
    # Actions
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 9 5-5 5 5"/><path d="M12 4v12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 11 5 5 5-5"/><path d="M12 16V4"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
    "send": '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
    "publish": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
    "arrow-right": '<path d="M5 12h13"/><path d="m12 5 7 7-7 7"/>',
    "arrow-left": '<path d="M19 12H6"/><path d="m12 19-7-7 7-7"/>',
    # Status
    "success": '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 16.5v.5"/>',
    "warning": '<path d="M10.3 3.9 2.5 17.2A2 2 0 0 0 4.2 20h15.6a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 16.5v.5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5v.5"/>',
    "empty": '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="m3 7 9 4 9-4"/><path d="M12 11v10"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "document": '<path d="M14 3v5h5"/><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9l5 5v11a2 2 0 0 1-2 2z"/>',
}

#: Which icon announces each Django message level.
MESSAGE_ICONS = {
    "success": "success",
    "error": "error",
    "warning": "warning",
    "info": "info",
    "debug": "info",
}


@register.simple_tag
def icon(name, size=15):
    """One registry icon, decorative by default.

    Icons here always sit beside their own text label, so they are hidden from
    assistive technology rather than being given a duplicate name to read out.
    """
    try:
        paths = ICONS[name]
    except KeyError:
        raise template.TemplateSyntaxError(
            f"Unknown icon {name!r}. Add it to platform_core/templatetags/icons.py."
        ) from None
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">{}</svg>',
        size,
        size,
        mark_safe(paths),  # noqa: S308 - literals from the registry above, never user input
    )


@register.simple_tag
def message_icon(tags):
    """The icon for a Django message's level, defaulting to informational."""
    for tag in (tags or "").split():
        if tag in MESSAGE_ICONS:
            return icon(MESSAGE_ICONS[tag])
    return icon("info")
