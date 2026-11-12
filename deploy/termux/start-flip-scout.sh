#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot autostart. Copy to ~/.termux/boot/ and chmod +x.
# Keeps the CPU awake, starts the SSH server and flip-scout inside proot-Ubuntu.
termux-wake-lock
sshd
proot-distro login ubuntu -- bash /root/flip-scout/deploy/termux/run-loop.sh \
    >> "$HOME/flip-scout-boot.log" 2>&1 &
