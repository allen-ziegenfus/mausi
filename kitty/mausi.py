"""kitty config for mausi, run by kitty itself: `geninclude ~/mausi/kitty/mausi.py`
in kitty.conf. Prints mausi.conf plus the lines that need this folder's path."""
import os

here = os.path.dirname(os.path.abspath(__file__))
print(open(os.path.join(here, "mausi.conf")).read())
print("# Alt-g: pick a tab by name (⏳ = a Claude session waiting on you).")
print(f"map alt+g kitten {os.path.join(here, 'picker.py')}")
# COSMIC: links clicked in kitty open behind it (Wayland gives kitty no way
# to raise the browser); mausi-open brings the browser to the front.
if "COSMIC" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
    print(f"open_url_with {os.path.join(os.path.dirname(here), 'src', 'mausi-open')}")
