#!/bin/sh
# Render every docs/diagrams/*.mmd (Mermaid source) to an SVG next to it.
#
# Usage:   sh scripts/render-diagrams.sh [--png DIR] [--pull]
#          (run it from anywhere; it works on the repo that contains this script)
#   --png DIR  also write a 2x PNG of each diagram to DIR (relative to the repo root),
#              for a quick look. Use .cache/diagrams-check, which is gitignored.
#              Handy for a quick look in an image viewer.
#   --pull     allow downloading the renderer image when it isn't present locally.
#              Without it the script stops instead, because the image is about 600 MB
#              compressed and is never downloaded by surprise.
#
# Why: the Markdown preview in VS Code (and many other viewers) doesn't render Mermaid
# code blocks, so the docs embed committed SVGs. Edit the .mmd, run this script, and
# commit both files.
#
# Runs in Docker only, with the official image pinned below, so it needs nothing else on
# the host. Works from Git Bash on Windows (Docker Desktop) and from Linux or macOS.
# Bump IMAGE deliberately and re-check every diagram afterwards (layouts can shift).
# The shared look (neutral theme, pure-SVG text, font, fixed seed so that renders are
# reproducible) is docs/diagrams/mermaid-config.json.
set -eu

IMAGE="minlag/mermaid-cli:11.16.0"

usage() { echo "usage: $0 [--png DIR] [--pull]" >&2; exit 2; }

png_dir=""
pull=0
while [ $# -gt 0 ]; do
    case $1 in
        --png) [ $# -ge 2 ] || usage; png_dir=$2; shift 2 ;;
        --pull) pull=1; shift ;;
        *) usage ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "docker not found: install Docker Desktop (or Docker Engine) first" >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    if [ "$pull" -eq 1 ]; then
        docker pull "$IMAGE"
    else
        echo "image $IMAGE is not present locally (about 600 MB to download)." >&2
        echo "Rerun with --pull to download it." >&2
        exit 3
    fi
fi

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"

# Git Bash: stop it from rewriting container paths, and mount a Windows-style path.
# Elsewhere: run as the calling user so the output files aren't owned by root or uid 1001.
if mount=$(pwd -W 2>/dev/null); then
    MSYS_NO_PATHCONV=1
    export MSYS_NO_PATHCONV
    user_args=""
else
    mount=$repo
    user_args="-u $(id -u):$(id -g)"
fi

set -- docs/diagrams/*.mmd
[ -e "$1" ] || { echo "no diagrams found in docs/diagrams" >&2; exit 1; }

# $user_args is deliberately unquoted: it is empty or two words without spaces.
render() { # render SOURCE OUTPUT [extra mmdc options...]
    src=$1
    out=$2
    shift 2
    # shellcheck disable=SC2086
    docker run --rm $user_args \
        --mount "type=bind,source=$mount,target=/data" \
        "$IMAGE" \
        -q -i "$src" -o "$out" -c docs/diagrams/mermaid-config.json -b white "$@"
}

[ -z "$png_dir" ] || mkdir -p "$png_dir"

for src in "$@"; do
    name=$(basename "$src" .mmd)
    echo "render $src -> docs/diagrams/$name.svg"
    render "$src" "docs/diagrams/$name.svg"
    if [ -n "$png_dir" ]; then
        render "$src" "$png_dir/$name.png" -s 2
    fi
done
