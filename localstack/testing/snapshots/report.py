import jsonpatch
import jsonpointer

from localstack.testing.snapshots import SnapshotMatchResult

_esctable = dict(
    # text colors
    black=30,
    red=31,
    green=32,
    yellow=33,
    blue=34,
    purple=35,
    cyan=36,
    white=37,
    # background colors
    Black=40,
    Red=41,
    Green=42,
    Yellow=43,
    Blue=44,
    Purple=45,
    Cyan=46,
    White=47,
    # special
    bold=1,
    light=2,
    blink=5,
    invert=7,
    strikethrough=9,
    underlined=4,
)


# maybe we can extend the methods in terminalwriter instead and use it directly?
def render_diff(result: SnapshotMatchResult):
    verified = result.a
    actual = result.b

    printstr = ""
    printstr += f">> {result.key}\n"
    patch = jsonpatch.make_patch(actual, verified)
    for p in patch.patch:
        appendstr = "\t"
        if p["op"] == "remove":
            # element at p['path'] shouldn't exist in actual but does
            appendstr += f"[remove](-)[/remove] {p['path']}"
        elif p["op"] == "add":
            # element at p['path'] of verified is missing in actual
            appendstr += (
                f"[add](+)[/add] {p['path']} ({jsonpointer.resolve_pointer(verified, p['path'])})"
            )
        elif p["op"] == "replace":
            # element at p['path'] exists in both verified and actual but with different values
            appendstr += f"[replace](~)[/replace] {p['path']} ([remove]{jsonpointer.resolve_pointer(actual, p['path'])}[/remove] → [add]{jsonpointer.resolve_pointer(verified, p['path'])}[/add])"
        else:
            raise ValueError(f"Unknown operation: {p}")
        printstr += f"{appendstr}\n"

    replacement_map = {
        "remove": [_esctable["red"]],
        "add": [_esctable["green"]],
        "replace": [_esctable["yellow"]],
    }

    for token, replacements in replacement_map.items():
        printstr = printstr.replace(f"[{token}]", "".join(f"\x1b[{code}m" for code in replacements))
        printstr = printstr.replace(f"[/{token}]", "\x1b[0m")

    return printstr
