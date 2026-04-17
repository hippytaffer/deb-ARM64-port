# deb-ARM64-port
Ports Debian files from Arch64 to ARM64

# Simple repackage (metadata only — works for script-based packages):
python3 deb_to_arm.py my-package_1.0_amd64.deb --arch arm64

# True cross-compilation from source:
apt source some-package
python3 deb_to_arm.py --source ./some-package-1.0/ --arch arm64

# Pre-requisites
sudo apt install dpkg-dev build-essential crossbuild-essential-arm64
