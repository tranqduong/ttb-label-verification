"""
Generates a handful of synthetic label images for demoing/testing the
verification app, since the take-home brief suggests sourcing or generating
test labels rather than requiring real product photography.

These are deliberately simple (no AI image generation dependency) but cover
the scenarios called out in the stakeholder interviews:

  1. clean_match          - everything matches the application exactly
  2. casing_mismatch_ok   - brand name differs only in case/punctuation
                             (Dave's "STONE'S THROW" vs "Stone's Throw" example
                             -- should still PASS)
  3. warning_titlecase_fail - government warning header in Title Case instead
                             of ALL CAPS (Jenny's real rejection example --
                             should FAIL)
  4. abv_mismatch_fail    - ABV on the label doesn't match the filed ABV
                             (should FAIL)
"""
import os
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

WARNING_STANDARD = (
    "GOVERNMENT WARNING: (1) ACCORDING TO THE SURGEON GENERAL, WOMEN SHOULD NOT DRINK\n"
    "ALCOHOLIC BEVERAGES DURING PREGNANCY BECAUSE OF THE RISK OF BIRTH DEFECTS. (2)\n"
    "CONSUMPTION OF ALCOHOLIC BEVERAGES IMPAIRS YOUR ABILITY TO DRIVE A CAR OR OPERATE\n"
    "MACHINERY, AND MAY CAUSE HEALTH PROBLEMS."
)

WARNING_TITLECASE = (
    "Government Warning: (1) According To The Surgeon General, Women Should Not Drink\n"
    "Alcoholic Beverages During Pregnancy Because Of The Risk Of Birth Defects. (2)\n"
    "Consumption Of Alcoholic Beverages Impairs Your Ability To Drive A Car Or Operate\n"
    "Machinery, And May Cause Health Problems."
)


def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_label(
    filename,
    brand_name="OLD TOM DISTILLERY",
    class_type="Kentucky Straight Bourbon Whiskey",
    abv="45% Alc./Vol. (90 Proof)",
    net_contents="750 mL",
    bottler="Old Tom Distillery, Bardstown, KY",
    warning_text=WARNING_STANDARD,
    warning_bold=True,
):
    width, height = 900, 1200
    img = Image.new("RGB", (width, height), "#f4ecd8")
    draw = ImageDraw.Draw(img)

    draw.rectangle([20, 20, width - 20, height - 20], outline="#3a2c1a", width=6)

    y = 90
    draw.text((width / 2, y), brand_name, font=_font(56, bold=True), fill="#2b1c0f", anchor="mm")
    y += 90
    draw.line([(100, y), (width - 100, y)], fill="#3a2c1a", width=2)
    y += 50
    draw.text((width / 2, y), class_type, font=_font(34), fill="#2b1c0f", anchor="mm")
    y += 90
    draw.text((width / 2, y), abv, font=_font(28), fill="#2b1c0f", anchor="mm")
    y += 60
    draw.text((width / 2, y), f"Net Contents: {net_contents}", font=_font(26), fill="#2b1c0f", anchor="mm")
    y += 70
    draw.text((width / 2, y), bottler, font=_font(22), fill="#2b1c0f", anchor="mm")

    y += 100
    draw.line([(60, y), (width - 60, y)], fill="#3a2c1a", width=1)
    y += 40

    warning_font = _font(18, bold=warning_bold)
    for line in warning_text.split("\n"):
        draw.text((width / 2, y), line, font=warning_font, fill="#1a1a1a", anchor="mm")
        y += 28

    path = os.path.join(OUT_DIR, filename)
    img.save(path, "JPEG", quality=92)
    print(f"wrote {path}")


if __name__ == "__main__":
    make_label(
        "clean_match.jpg",
        brand_name="OLD TOM DISTILLERY",
        class_type="Kentucky Straight Bourbon Whiskey",
        abv="45% Alc./Vol. (90 Proof)",
        net_contents="750 mL",
        bottler="Old Tom Distillery, Bardstown, KY",
        warning_text=WARNING_STANDARD,
        warning_bold=True,
    )

    make_label(
        "casing_mismatch_ok.jpg",
        brand_name="STONE'S THROW",
        class_type="American Craft Vodka",
        abv="40% Alc./Vol.",
        net_contents="750 mL",
        bottler="Stone's Throw Distilling Co.",
        warning_text=WARNING_STANDARD,
        warning_bold=True,
    )

    make_label(
        "warning_titlecase_fail.jpg",
        brand_name="HARBOR LIGHT BREWING",
        class_type="India Pale Ale",
        abv="6.5% Alc./Vol.",
        net_contents="355 mL",
        bottler="Harbor Light Brewing Co., Portland, ME",
        warning_text=WARNING_TITLECASE,
        warning_bold=False,
    )

    make_label(
        "abv_mismatch_fail.jpg",
        brand_name="SILVER RIDGE VINEYARDS",
        class_type="Cabernet Sauvignon",
        abv="14.5% Alc./Vol.",
        net_contents="750 mL",
        bottler="Silver Ridge Vineyards, Napa, CA",
        warning_text=WARNING_STANDARD,
        warning_bold=True,
    )
