"""Genere l'icone de NOMAIA (PNG pour la barre des taches, ICO pour les raccourcis).

Ecrit aussi les icones de l'ecran d'accueil du telephone (manifeste web et
apple-touch-icon) : carre plein, sans transparence, la lettre dans la zone
sure de 80 % que les lanceurs Android rognent en cercle ou en goutte.

A relancer uniquement si l'identite visuelle change :
    python tools/make_icon.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

ACCENT = (99, 102, 241, 255)
WHITE = (255, 255, 255, 255)
SIZE = 256


# Icones de l'ecran d'accueil : nom de fichier et cote en pixels.
APP_ICONS = (("app-192.png", 192), ("app-512.png", 512), ("apple-touch-icon.png", 180))


def build_image(size=SIZE, full_bleed=False, letter_ratio=0.62):
    """Dessine un carre indigo portant la lettre N.

    :param full_bleed: carre plein (icone d'ecran d'accueil, que le systeme
        arrondit lui-meme) plutot qu'un carre arrondi sur fond transparent
    :param letter_ratio: hauteur de la lettre rapportee au cote
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if full_bleed:
        draw.rectangle([0, 0, size - 1, size - 1], fill=ACCENT)
    else:
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=ACCENT)

    try:
        font = ImageFont.truetype("segoeuib.ttf", int(size * letter_ratio))
    except OSError:
        font = ImageFont.load_default()

    box = draw.textbbox((0, 0), "N", font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        "N",
        font=font,
        fill=WHITE,
    )
    return image


def main():
    """Ecrit icon.png, icon.ico et les icones d'ecran d'accueil dans static/img."""
    folder = config.BASE_DIR / "static" / "img"
    folder.mkdir(parents=True, exist_ok=True)

    image = build_image()
    image.save(folder / "icon.png")
    image.save(
        folder / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    for name, size in APP_ICONS:
        build_image(size, full_bleed=True, letter_ratio=0.5).convert("RGB").save(folder / name)
    print("Icones ecrites dans", folder)


if __name__ == "__main__":
    main()
