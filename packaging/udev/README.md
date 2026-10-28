# udev rule

`99-upvc-ftdi.rules` gives the `plugdev` group read/write access to FTDI
0403:6010 (FT2232H) and 0403:6011 (FT4232H) devices, so `upvc` runs without root.

    sudo install -m 0644 packaging/udev/99-upvc-ftdi.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger
    sudo usermod -aG plugdev "$USER"      # then log out and back in

The .deb installs the rule for you. The rule covers every device with those IDs,
not only the cards; unplug and replug the cable after installing it.
