"""Small helpers for drawing outlined text and see-through panels."""
import pygame

_text_cache = {}


def outlined(font, text, color, outline=2, outline_color=(0, 0, 0)):
    """Render text with a dark outline (cached, so it is cheap to call every frame)."""
    key = (id(font), text, tuple(color), outline, tuple(outline_color))
    image = _text_cache.get(key)
    if image is None:
        base = font.render(text, True, color)
        if outline:
            edge = font.render(text, True, outline_color)
            image = pygame.Surface((base.get_width() + outline * 2, base.get_height() + outline * 2),
                                   pygame.SRCALPHA)
            for dx in (-outline, 0, outline):
                for dy in (-outline, 0, outline):
                    if dx or dy:
                        image.blit(edge, (dx + outline, dy + outline))
            image.blit(base, (outline, outline))
        else:
            image = base
        if len(_text_cache) > 500:
            _text_cache.clear()
        _text_cache[key] = image
    return image


def draw_text(surface, text, font, color, pos, anchor="center", outline=2, outline_color=(0, 0, 0)):
    """Draw text positioned by any rect anchor ("center", "midleft", "topright", ...)."""
    image = outlined(font, text, color, outline, outline_color)
    rect = image.get_rect(**{anchor: (round(pos[0]), round(pos[1]))})
    surface.blit(image, rect)
    return rect


def draw_panel(surface, rect, color=(15, 15, 25), alpha=180, border=None, width=2, radius=10):
    """A rounded, see-through box with an optional border."""
    rect = pygame.Rect(rect)
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(panel, (*color, alpha), panel.get_rect(), border_radius=radius)
    surface.blit(panel, rect.topleft)
    if border:
        pygame.draw.rect(surface, border, rect, width, border_radius=radius)


def wrap_text(text, font, max_width):
    """Split text into lines that fit inside max_width pixels."""
    lines, current = [], ""
    for word in text.split():
        attempt = f"{current} {word}".strip()
        if font.size(attempt)[0] <= max_width or not current:
            current = attempt
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
