"""Path-map checks for the Windows-Tdarr / Linux-Docker-Stash topology."""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "stash-plugin", "tdarrSync", "tdarrSync.py")

spec = importlib.util.spec_from_file_location("tdarrsync", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

RULES = mod.parse_path_map(
    "Z:\\Media=>/data"
    "|\\\\NAS\\share\\tv=>/tv"
    "|/srv/local=>/mnt/local"
)

CASES = [
    # (input path, expected output, why)
    ("Z:\\Media\\Movies\\foo.mkv", "/data/Movies/foo.mkv", "drive letter maps"),
    ("z:\\media\\movies\\FOO.mkv", "/data/movies/FOO.mkv", "drive letter is case-insensitive"),
    ("Z:/Media/Movies/foo.mkv", "/data/Movies/foo.mkv", "forward slashes on Windows too"),
    ("\\\\NAS\\share\\tv\\s01e01.mkv", "/tv/s01e01.mkv", "UNC maps"),
    ("\\\\nas\\SHARE\\TV\\s01e01.mkv", "/tv/s01e01.mkv", "UNC is case-insensitive"),
    ("/srv/local/a.mkv", "/mnt/local/a.mkv", "linux rule maps"),
    ("/SRV/LOCAL/a.mkv", "/SRV/LOCAL/a.mkv", "linux rules stay case-sensitive"),
    ("Z:\\MediaExtra\\x.mkv", "Z:\\MediaExtra\\x.mkv", "no partial-segment match"),
    ("D:\\Other\\x.mkv", "D:\\Other\\x.mkv", "unmapped drive passes through"),
]


def main():
    print("rules: {}".format(RULES))
    failures = 0
    for raw, expected, why in CASES:
        actual = mod.map_path(raw, RULES)
        ok = actual == expected
        failures += 0 if ok else 1
        print(
            "  {} {!r}\n      -> {!r}{}".format(
                "PASS" if ok else "FAIL",
                raw,
                actual,
                "" if ok else "   EXPECTED {!r}".format(expected),
            )
        )
        print("      ({})".format(why))
    print("\n{} passed, {} failed".format(len(CASES) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
