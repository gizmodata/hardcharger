"""Select a published stable predecessor and the long-term reference releases for a server tag."""

import re
import sys


def baselines(candidate, published, anchors):
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)(?:-rc\.\d+)?", candidate)
    if not match:
        raise ValueError(f"unexpected server tag {candidate!r}")

    def version(text):
        return tuple(map(int, text.split(".")))

    target = version(match[1])
    earlier = [tag[1:] for tag in published if re.fullmatch(r"v\d+\.\d+\.\d+", tag)
               and version(tag[1:]) < target]
    if not earlier:
        raise ValueError(f"no published stable predecessor for {candidate}")
    return list(dict.fromkeys([a for a in anchors if version(a) < target] + [max(earlier, key=version)]))


if __name__ == "__main__":
    print(" ".join(baselines(sys.argv[1], sys.stdin.read().split(), sys.argv[2:])))
