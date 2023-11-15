import shutil
import os
import json
import sys
import subprocess
import tempfile
import platform
import re

from platformio.package.manager.tool import ToolPackageManager

# CMake (> 3.15) and Ninja deps are pulled from PlatformIO Registry
REQUIRED_PREREQUISITE_APPS = ("cmake", "ninja", "ldd", "platformio", "gcc", "g++", "ldd")
PIO_PKG_MANAGER = ToolPackageManager()

# Folder that will be used to compile files
BUILD_DIR = os.path.join(os.getcwd(), "build")

# Folder that will contain built packages
RESULT_DIR = os.path.join(os.getcwd(), "result")

# Used for installing local CMake and Ninja

IS_WINDOWS = platform.system().lower().startswith("win")

def is_program_installed(program_name):
    return shutil.which(program_name)


def check_requirements(build_dir):
    cmake_dir = os.path.join(get_piopkg_dir("tool-cmake"), "bin")
    ninja_dir = os.path.join(get_piopkg_dir("tool-ninja"))
    os.environ["PATH"] = os.pathsep.join([cmake_dir, ninja_dir] + [os.environ["PATH"]])

    for requirement in REQUIRED_PREREQUISITE_APPS:
        assert is_program_installed(requirement), "'%s' is not installed!" % requirement

    for d in (build_dir, RESULT_DIR):
        if not os.path.isdir(d):
            os.makedirs(d)
            assert os.path.isdir(d), "The folder `%s` was not created!"

    # Clean build directory for the next build
    # if os.path.isdir(build_dir):
    #     shutil.rmtree(build_dir, ignore_errors=False)


def normalize_binary(binary_name):
    return binary_name + ".exe" if IS_WINDOWS else binary_name


def exec_command(args):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    exitcode = proc.returncode
    return {"returncode": exitcode, "out": out.decode("utf-8"), "err": err}


def validate_exec_command(result, on_err_msg="Failed!"):
    if result["returncode"] != 0:
        print(on_err_msg)
        print(result["out"])
        print(result["err"])
        sys.exit(1)


def get_piopkg_dir(package_name):
    pkg = PIO_PKG_MANAGER.get_package(package_name)
    if not pkg:
        pkg = PIO_PKG_MANAGER.install(package_name)

    return pkg.path


def run_cmake(args=None):
    args = args or tuple()
    res = exec_command(("cmake",) + args)
    validate_exec_command(res, "CMake failed to run with args %s" % " ".join(args))


def configure_cmake_project(build_dir, install_dir, cmake_extra_flags=None):
    print("Configuring CMake...")
    cmake_args = (
        "-B",
        build_dir,
        "-S",
        ".",
        "-G",
        "Ninja",
        "-DBUILD_SHARED_LIBS=NO",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DUSE_MATCHCOMPILER=ON",
        "-DFILESDIR=%s" % install_dir,
        "-DCMAKE_INSTALL_PREFIX:PATH=%s" % install_dir,
    )

    run_cmake(cmake_args)


def install_cppcheck(build_dir):
    print("Building and installing project...")
    cmake_args = ("--build", build_dir, "--target", "install")
    run_cmake(cmake_args)


def posix2win(path):
    # quick and dirty way
    result = path[1] + ":" + path[2:]
    print("Converted `%s` to `%s`" % (path, result))
    return result


def extract_dynamic_libraries(binary_path, allowed_paths=None):
    allowed_paths = allowed_paths or []
    libs = []

    assert os.path.isfile(binary_path), "%s binary is not found" % binary_path
    res = exec_command(("ldd", binary_path))
    if res["returncode"] == 0:
        pattern = r"^.+(?:dll|DLL) => (?P<lib_path>.*) \((?:.*)\)$"
        prog = re.compile(pattern)
        for line in res["out"].split("\n"):
            line = line.strip()
            if not line:
                continue

            match = prog.match(line)
            if not match:
                print("Warning! No libraries were found! in %s" % res["out"])
            lib_path = posix2win(match.group("lib_path"))
            assert os.path.isfile(lib_path), (
                "Dynamic library `%s` doesn't exit" % lib_path
            )

            if any(path in lib_path for path in allowed_paths):
                libs.append(lib_path)

    return libs


def copy_msys_lib_deps(binary_path, allowed_paths=None):
    dst_path = os.path.dirname(binary_path)
    for lib_path in extract_dynamic_libraries(binary_path, allowed_paths):
        print("Copying `%s` to `%s`" % (lib_path, dst_path))
        shutil.copyfile(lib_path, os.path.join(dst_path, os.path.basename(lib_path)))


def prepare_package(install_dir):
    assert os.listdir(install_dir), "Error: folder with package cannot be empty!"

    print("Preparing package...")
    # Copy binary from the "bin" folder to the root
    binary_name = normalize_binary("cppcheck")
    binary_path = os.path.join(install_dir, "bin", binary_name)
    assert os.path.isfile(
        binary_path
    ), "Missing cppcheck binary in the installed directory"
    shutil.move(binary_path, os.path.join(install_dir, binary_name))
    binary_path = os.path.join(install_dir, binary_name)

    # Delete the empty "bin" folder
    shutil.rmtree(os.path.join(install_dir, "bin"), ignore_errors=False)

    # copy readme if available
    for f in os.listdir(os.getcwd()):
        f = f.lower()
        if f in ("readme.md", "readme.txt"):
            shutil.copy(os.path.join(os.getcwd(), f), os.path.join(install_dir, f))
            break

    if IS_WINDOWS:
        print("Copying MSYS dynamic libraries for Windows")
        copy_msys_lib_deps(binary_path, allowed_paths=["msys64"])


def validate_package(install_dir):
    print("Validating package structure...")
    # Check if binary is available
    assert os.path.isfile(
        os.path.join(install_dir, normalize_binary("cppcheck"))
    ), "Missing cppcheck binary in the package folder:"

    # Check extra folders with scripts and addons
    for folder in ("addons", "cfg", "platforms"):
        assert os.path.isdir(os.path.join(install_dir, folder)), (
            "%s folder is missing!" % folder
        )

    # # Check the "bin" folder doesn't exist
    # assert os.path.isdir(os.path.join(install_dir, "bin"))

    # Check PlatformIO Manifest
    assert os.path.isfile(
        os.path.join(install_dir, "package.json")
    ), "Missing PlatformIO manifest file"

    # Check if binary is alive
    res = exec_command(
        [os.path.join(install_dir, normalize_binary("cppcheck")), "--version"]
    )
    validate_exec_command(res, "Failed to validate final viable binary")


def create_pio_package(package_dir, result_dir):
    print(
        "Preparing a PlatformIO package from `%s` to `%s`" % (package_dir, result_dir)
    )
    assert os.path.isdir(package_dir), "Package folder doesn't exist"

    pio_args = ("pio", "package", "pack", package_dir, "-o", result_dir)
    res = exec_command(pio_args)
    validate_exec_command(res, "Failed to create a PlatformIO package!")


def archive_package(package_dir, result_dir):
    print("Preparing an archive package from `%s` to `%s`" % (package_dir, result_dir))
    assert os.path.isdir(package_dir), "Package folder doesn't exist"

    backup_cwd = os.getcwd()
    os.chdir(package_dir)
    tar_args = (
        "tar",
        "-czf",
        os.path.join(result_dir, "cppcheck-%s.tar.gz" % get_target_systems()),
        " ".join(os.listdir(package_dir)),
    )
    res = exec_command(tar_args)

    os.chdir(backup_cwd)
    validate_exec_command(res, "Failed to create an archive package!")


def get_target_systems():
    default = "%s_%s" % (platform.system().lower(), platform.machine().lower())
    target_systems = os.environ.get("PLATFORMIO_PACKAGE_SYSTEM_VALUES", "")
    if not target_systems:
        print(
            "Warning! The env variable `PLATFORMIO_PACKAGE_SYSTEM_VALUES` is not set!"
        )
        return [default]

    # Return cleaned system values
    return [value.strip().replace('"', "") for value in target_systems.split(",")]


def convert_version_to_pio_compatible(version):
    version = version.replace("v", "")
    if version.count(".") == 1:
        version = version + ".0"
    major, minor, patch = version.split(".")
    version = major
    if len(minor) < 2:
        version = version + "0" + minor
    else:
        version = version + minor
    if len(patch) < 2:
        version = version + "0" + patch
    else:
        version = version + patch

    return "1." + version + ".0"


def extract_version_from_git_env():
    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref:
        original_version = github_ref.replace("refs/tags/", "")
        if original_version:
            return original_version
        else:
            print("Warning! There is no refs/tags/* value in $GITHUB_REF")
    else:
        print(
            "Warning! GITHUB_REF is not available. Extracting version directly form Git..."
        )
        # "git describe --tags  --match"
        pio_args = ("git", "describe", "--tags")
        res = exec_command(pio_args)
        if res["returncode"] == 0:
            return res["out"].strip()

    print(
        "Warning! Failed to extract version value from the `GITHUB_REF` variable! "
        "Default '1.0.0` will be used!'"
    )
    print(str(res["err"]))
    return "1.0.0"


def get_cppcheck_version():
    return os.environ.get("PLATFORMIO_PACKAGE_VERSION", extract_version_from_git_env())


def get_piopkg_cppcheck_version(version):
    return convert_version_to_pio_compatible(version)


def get_package_manifeset_data(version, system):

    assert version, "Version value cannot be empty"
    assert system, "Version value cannot be empty"

    return {
        "name": "tool-cppcheck",
        "version": version,
        "description": "Static code analysis tool for the C and C++ programming languages",
        "keywords": ["static analysis", "tools"],
        "homepage": "http://cppcheck.sourceforge.net",
        "license": "GPL-3.0-or-later",
        "system": system,
        "repository": {"type": "git", "url": "https://github.com/danmar/cppcheck"},
    }


def generate_pio_manifest(result_dir, version, system):
    print("Generating PlatformIO manifest file for '%s' v%s" % (system, version))
    pkg_manifest = os.path.join(result_dir, "package.json")

    manifest_data = get_package_manifeset_data(version, system)
    with open(pkg_manifest, "w") as fp:
        json.dump(manifest_data, fp, indent=2)


def main():
    package_systems = get_target_systems()
    cppcheck_version = get_cppcheck_version()
    pio_pkg_version = get_piopkg_cppcheck_version(cppcheck_version)
    build_dir = BUILD_DIR + "-_".join(package_systems)

    check_requirements(build_dir)
    print(
        "Building Cppcheck with version '%s' for '%s'"
        % (pio_pkg_version, ",".join(package_systems))
    )

    with tempfile.TemporaryDirectory() as install_dir:
        install_dir = os.path.join(
            os.getcwd(), "cppcheck-built-install-dir-" + "_".join(package_systems)
        )
        print("Cppcheck will be installed to '%s'" % install_dir)

        # Build and prepare package that will be added to the PlatformIO Registry
        configure_cmake_project(build_dir, install_dir)
        install_cppcheck(build_dir)
        prepare_package(install_dir)

        # # Generate PlatformIO-specific files
        generate_pio_manifest(install_dir, pio_pkg_version, package_systems)

        # Make sure package is working and ready to be archived
        validate_package(install_dir)

        # Archive the package
        # archive_package(install_dir, RESULT_DIR)

        # Or Make a PlatformIO package
        create_pio_package(install_dir, RESULT_DIR)

        assert os.path.isfile(
            os.path.join(
                RESULT_DIR,
                "tool-cppcheck-%s-%s.tar.gz" % (package_systems[0], pio_pkg_version),
            )
        ), "The final PlatformIO package is missing!"


if __name__ == "__main__":
    main()
