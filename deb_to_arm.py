#!/usr/bin/env python3
"""
Cross-architecture .deb package rebuilder: amd64 → ARM (arm64/armhf)

This script automates rebuilding Debian packages for ARM architectures.
It extracts source from an amd64 .deb, sets up a cross-compilation
environment, and rebuilds for the target ARM architecture.

Requirements:
    - dpkg-dev, devscripts, build-essential
    - crossbuild-essential-arm64 or crossbuild-essential-armhf
    - pbuilder or sbuild (optional, for clean builds)

Usage:
    python3 deb_to_arm.py <package.deb> --arch arm64
    python3 deb_to_arm.py <package.deb> --arch armhf --output ./output
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run_command(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """
    Execute a shell command and return the result.
    
    Args:
        cmd: Command and arguments as a list
        cwd: Working directory for the command
        check: Raise exception on non-zero exit code
    
    Returns:
        CompletedProcess instance with stdout/stderr
    """
    print(f"  → Running: {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True
    )


def check_dependencies() -> bool:
    """
    Verify that required tools are installed on the system.
    
    Returns:
        True if all dependencies are met, False otherwise
    """
    required_tools = [
        "dpkg-deb",      # Extract and build .deb packages
        "dpkg-source",   # Handle Debian source packages
        "dpkg-buildpackage",  # Build packages from source
        "ar",            # Archive tool for .deb manipulation
    ]
    
    missing = []
    for tool in required_tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    
    if missing:
        print(f"Error: Missing required tools: {', '.join(missing)}")
        print("Install with: sudo apt install dpkg-dev build-essential")
        return False
    
    return True


def install_cross_compiler(target_arch: str) -> bool:
    """
    Check if cross-compilation toolchain is available for target architecture.
    
    Args:
        target_arch: Target architecture (arm64 or armhf)
    
    Returns:
        True if cross-compiler is available
    """
    # Map Debian arch names to GNU triplets
    triplet_map = {
        "arm64": "aarch64-linux-gnu",
        "armhf": "arm-linux-gnueabihf",
    }
    
    triplet = triplet_map.get(target_arch)
    if not triplet:
        print(f"Error: Unsupported architecture '{target_arch}'")
        return False
    
    # Check if cross-compiler exists
    cross_gcc = f"{triplet}-gcc"
    if shutil.which(cross_gcc) is None:
        print(f"Warning: Cross-compiler '{cross_gcc}' not found.")
        print(f"Install with: sudo apt install crossbuild-essential-{target_arch}")
        return False
    
    return True


def extract_deb(deb_path: Path, work_dir: Path) -> Path:
    """
    Extract a .deb package into its components.
    
    A .deb file is an 'ar' archive containing:
        - debian-binary: Version info
        - control.tar.*: Package metadata
        - data.tar.*: Actual files
    
    Args:
        deb_path: Path to the .deb file
        work_dir: Directory to extract into
    
    Returns:
        Path to the extracted package directory
    """
    extract_dir = work_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)
    
    # Extract the .deb archive
    print(f"\n[1/5] Extracting {deb_path.name}...")
    run_command(["dpkg-deb", "-R", str(deb_path), str(extract_dir)])
    
    return extract_dir


def modify_control_file(extract_dir: Path, target_arch: str) -> dict:
    """
    Update the DEBIAN/control file with new architecture.
    
    The control file contains package metadata including:
        - Package name, version, description
        - Architecture (what we're changing)
        - Dependencies
    
    Args:
        extract_dir: Path to extracted package
        target_arch: New target architecture
    
    Returns:
        Dictionary of parsed control file fields
    """
    control_path = extract_dir / "DEBIAN" / "control"
    
    if not control_path.exists():
        raise FileNotFoundError(f"Control file not found: {control_path}")
    
    print(f"\n[2/5] Modifying control file for {target_arch}...")
    
    # Parse existing control file
    control_data = {}
    current_key = None
    
    with open(control_path, "r") as f:
        for line in f:
            if line.startswith(" ") or line.startswith("\t"):
                # Continuation of previous field
                if current_key:
                    control_data[current_key] += "\n" + line.rstrip()
            elif ":" in line:
                key, value = line.split(":", 1)
                current_key = key.strip()
                control_data[current_key] = value.strip()
    
    # Store original architecture for reference
    original_arch = control_data.get("Architecture", "unknown")
    print(f"    Original architecture: {original_arch}")
    
    # Update architecture field
    control_data["Architecture"] = target_arch
    print(f"    New architecture: {target_arch}")
    
    # Write modified control file
    with open(control_path, "w") as f:
        # Maintain standard field order for control files
        field_order = [
            "Package", "Version", "Architecture", "Maintainer",
            "Installed-Size", "Depends", "Pre-Depends", "Recommends",
            "Suggests", "Conflicts", "Provides", "Replaces",
            "Section", "Priority", "Homepage", "Description"
        ]
        
        written_fields = set()
        
        # Write fields in standard order
        for field in field_order:
            if field in control_data:
                f.write(f"{field}: {control_data[field]}\n")
                written_fields.add(field)
        
        # Write any remaining fields
        for field, value in control_data.items():
            if field not in written_fields:
                f.write(f"{field}: {value}\n")
    
    return control_data


def check_binary_files(extract_dir: Path, target_arch: str) -> list[Path]:
    """
    Scan for architecture-specific binary files that need recompilation.
    
    ELF binaries compiled for amd64 won't run on ARM. This function
    identifies such files so the user knows what needs rebuilding.
    
    Args:
        extract_dir: Path to extracted package
        target_arch: Target architecture
    
    Returns:
        List of paths to binary files found
    """
    print("\n[3/5] Scanning for architecture-specific binaries...")
    
    binary_files = []
    
    # Walk through all files in the package
    for file_path in extract_dir.rglob("*"):
        if not file_path.is_file():
            continue
        
        # Skip the DEBIAN control directory
        if "DEBIAN" in file_path.parts:
            continue
        
        # Check if file is an ELF binary using the 'file' command
        try:
            result = run_command(["file", "-b", str(file_path)], check=False)
            file_type = result.stdout.strip()
            
            # Look for ELF executables and shared libraries
            if "ELF" in file_type:
                binary_files.append(file_path)
                
                # Determine current architecture of the binary
                if "x86-64" in file_type or "x86_64" in file_type:
                    arch_info = "amd64"
                elif "ARM aarch64" in file_type:
                    arch_info = "arm64"
                elif "ARM," in file_type:
                    arch_info = "armhf"
                else:
                    arch_info = "unknown"
                
                relative_path = file_path.relative_to(extract_dir)
                print(f"    Found binary ({arch_info}): {relative_path}")
                
        except Exception:
            pass  # Skip files we can't analyze
    
    return binary_files


def rebuild_package(extract_dir: Path, output_dir: Path, control_data: dict) -> Path:
    """
    Rebuild the .deb package from the extracted directory.
    
    Note: This creates a new .deb with modified metadata, but does NOT
    recompile binaries. For true cross-compilation, you need source code.
    
    Args:
        extract_dir: Path to extracted/modified package
        output_dir: Where to save the new .deb
        control_data: Package metadata dictionary
    
    Returns:
        Path to the newly created .deb file
    """
    print("\n[4/5] Rebuilding package...")
    
    # Generate output filename based on package metadata
    package_name = control_data.get("Package", "unknown")
    version = control_data.get("Version", "0.0.0")
    arch = control_data.get("Architecture", "all")
    
    output_filename = f"{package_name}_{version}_{arch}.deb"
    output_path = output_dir / output_filename
    
    # Build the .deb package
    run_command([
        "dpkg-deb",
        "--build",
        "--root-owner-group",  # Use root as owner (standard for .debs)
        str(extract_dir),
        str(output_path)
    ])
    
    print(f"    Created: {output_path}")
    
    return output_path


def print_summary(
    original_deb: Path,
    output_deb: Path,
    binary_files: list[Path],
    target_arch: str
) -> None:
    """
    Print a summary of the conversion process and any warnings.
    """
    print("\n" + "=" * 60)
    print("[5/5] CONVERSION SUMMARY")
    print("=" * 60)
    print(f"  Source package:  {original_deb.name}")
    print(f"  Output package:  {output_deb.name}")
    print(f"  Target arch:     {target_arch}")
    print(f"  Output size:     {output_deb.stat().st_size / 1024:.1f} KB")
    
    if binary_files:
        print("\n⚠️  WARNING: This package contains compiled binaries!")
        print("   The following files are architecture-specific and need")
        print("   to be recompiled from source for true ARM compatibility:")
        print()
        for bf in binary_files[:5]:  # Show first 5
            print(f"     • {bf.name}")
        if len(binary_files) > 5:
            print(f"     ... and {len(binary_files) - 5} more")
        
        print("\n   OPTIONS FOR TRUE CROSS-COMPILATION:")
        print("   1. Obtain source package: apt source <package-name>")
        print("   2. Cross-compile with: dpkg-buildpackage -a{target_arch}")
        print("   3. Or use QEMU + pbuilder for native ARM build")
    else:
        print("\n✓ Package appears to be architecture-independent (no binaries)")
        print("  The converted package should work directly on ARM.")


def convert_deb_to_arm(
    deb_path: str,
    target_arch: str,
    output_dir: str | None = None
) -> Path | None:
    """
    Main conversion function: repackage a .deb for ARM architecture.
    
    IMPORTANT LIMITATIONS:
    This script changes the architecture METADATA of a .deb package.
    It does NOT recompile code. For packages containing compiled binaries:
    
    - Architecture-independent packages (Python, shell scripts, docs): ✓ Will work
    - Packages with compiled binaries: ✗ Need source recompilation
    
    For true cross-compilation of binary packages, you need:
    1. Source code (apt source <package>)
    2. Cross-compilation toolchain (crossbuild-essential-arm64)
    3. Build with: dpkg-buildpackage -aarm64 -b
    
    Args:
        deb_path: Path to source .deb file
        target_arch: Target architecture (arm64, armhf)
        output_dir: Output directory (default: current directory)
    
    Returns:
        Path to converted .deb file, or None on failure
    """
    deb_path = Path(deb_path).resolve()
    output_dir = Path(output_dir or ".").resolve()
    
    # Validate inputs
    if not deb_path.exists():
        print(f"Error: File not found: {deb_path}")
        return None
    
    if not deb_path.suffix == ".deb":
        print(f"Error: Not a .deb file: {deb_path}")
        return None
    
    if target_arch not in ("arm64", "armhf"):
        print(f"Error: Unsupported architecture: {target_arch}")
        print("Supported: arm64, armhf")
        return None
    
    # Check system dependencies
    if not check_dependencies():
        return None
    
    # Check for cross-compiler (warn only, don't fail)
    install_cross_compiler(target_arch)
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Use temporary directory for extraction
    with tempfile.TemporaryDirectory(prefix="deb_arm_") as temp_dir:
        work_dir = Path(temp_dir)
        
        try:
            # Step 1: Extract the .deb
            extract_dir = extract_deb(deb_path, work_dir)
            
            # Step 2: Modify control file
            control_data = modify_control_file(extract_dir, target_arch)
            
            # Step 3: Check for binaries
            binary_files = check_binary_files(extract_dir, target_arch)
            
            # Step 4: Rebuild package
            output_deb = rebuild_package(extract_dir, output_dir, control_data)
            
            # Step 5: Print summary
            print_summary(deb_path, output_deb, binary_files, target_arch)
            
            return output_deb
            
        except subprocess.CalledProcessError as e:
            print(f"\nError during conversion: {e}")
            print(f"Command output: {e.stderr}")
            return None
        except Exception as e:
            print(f"\nUnexpected error: {e}")
            return None


def cross_compile_from_source(
    source_dir: str,
    target_arch: str,
    output_dir: str | None = None
) -> Path | None:
    """
    Cross-compile a Debian source package for ARM.
    
    This performs TRUE cross-compilation, producing native ARM binaries.
    Requires the source package and cross-compilation toolchain.
    
    Args:
        source_dir: Path to unpacked Debian source directory
        target_arch: Target architecture (arm64, armhf)
        output_dir: Where to place built packages
    
    Returns:
        Path to output directory containing built packages
    """
    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir or source_dir.parent).resolve()
    
    if not (source_dir / "debian").exists():
        print(f"Error: Not a Debian source directory: {source_dir}")
        print("Expected 'debian/' subdirectory with packaging files")
        return None
    
    print(f"\nCross-compiling for {target_arch}...")
    print(f"Source: {source_dir}")
    
    # Set up cross-compilation environment variables
    triplet_map = {
        "arm64": "aarch64-linux-gnu",
        "armhf": "arm-linux-gnueabihf",
    }
    triplet = triplet_map[target_arch]
    
    env = os.environ.copy()
    env.update({
        "CC": f"{triplet}-gcc",
        "CXX": f"{triplet}-g++",
        "PKG_CONFIG": f"{triplet}-pkg-config",
        "DEB_BUILD_OPTIONS": "nocheck",  # Skip tests (can't run ARM on x86)
    })
    
    try:
        # Run dpkg-buildpackage with cross-compilation flags
        result = subprocess.run(
            [
                "dpkg-buildpackage",
                f"-a{target_arch}",   # Target architecture
                "-b",                  # Binary-only build
                "-uc",                 # Don't sign changes
                "-us",                 # Don't sign source
                "--no-check-builddeps",  # Skip build-dep check (may need adjustment)
            ],
            cwd=source_dir,
            env=env,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"Build failed:\n{result.stderr}")
            return None
        
        print(f"\n✓ Build complete! Packages in: {output_dir}")
        return output_dir
        
    except Exception as e:
        print(f"Error during cross-compilation: {e}")
        return None


def main():
    """Parse arguments and run the conversion."""
    parser = argparse.ArgumentParser(
        description="Convert amd64 .deb packages for ARM architectures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Repackage for arm64 (metadata only):
  %(prog)s package_amd64.deb --arch arm64

  # Specify output directory:
  %(prog)s package_amd64.deb --arch armhf --output ./arm-packages

  # Cross-compile from source (true recompilation):
  %(prog)s --source ./package-1.0/ --arch arm64

Notes:
  - Simple repackaging changes metadata but NOT compiled binaries
  - For binary packages, use --source with actual source code
  - Install cross-compilers: apt install crossbuild-essential-arm64
        """
    )
    
    parser.add_argument(
        "deb_file",
        nargs="?",
        help="Path to the .deb file to convert"
    )
    
    parser.add_argument(
        "--arch", "-a",
        required=True,
        choices=["arm64", "armhf"],
        help="Target ARM architecture"
    )
    
    parser.add_argument(
        "--output", "-o",
        default=".",
        help="Output directory (default: current directory)"
    )
    
    parser.add_argument(
        "--source", "-s",
        help="Path to source directory for true cross-compilation"
    )
    
    args = parser.parse_args()
    
    # Determine mode: source compilation or .deb repackaging
    if args.source:
        # Cross-compile from source
        result = cross_compile_from_source(args.source, args.arch, args.output)
    elif args.deb_file:
        # Repackage existing .deb
        result = convert_deb_to_arm(args.deb_file, args.arch, args.output)
    else:
        parser.error("Either a .deb file or --source directory is required")
        return 1
    
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
