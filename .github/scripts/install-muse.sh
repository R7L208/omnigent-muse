#!/usr/bin/env bash
# Install the pinned Muse CLI for the integration workflow.
#
# Downloads one release binary straight from Muse's release storage and
# verifies it against the sha256 pinned below, instead of piping the
# self-updating installer (`curl ... | bash`) into a shell. The checksum is
# verified on every run, including when the download was restored from cache.
# Pins come from the release manifest,
#   https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=<VERSION>&file=manifest.json
# (artifacts.<platform>.checksum); bump all three values together.
#
# Env in:  MUSE_INSTALL_DIR (default: $RUNNER_TEMP/muse, else ~/.local/bin).
# Env out: OMNIGENT_MUSE_PATH and MUSE_NO_AUTO_UPDATE, appended to
#          $GITHUB_ENV when set.

set -euo pipefail

MUSE_VERSION="1.4.8-R6256.1"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="muse-x86-linux"
    sha256="416eaa520e6bf98c7d67f8d2772a2464d41839c1cb7a75d299845400f484fd4b"
    ;;
  Linux:aarch64 | Linux:arm64)
    artifact="muse-aarch64-linux"
    sha256="592aabe306186794e05d9329ccef7d49116abf0a8bcbc1e07703d22ef1b34f3a"
    ;;
  *)
    echo "::error::no pinned Muse artifact for $(uname -s) $(uname -m)" >&2
    exit 1
    ;;
esac

install_dir="${MUSE_INSTALL_DIR:-${RUNNER_TEMP:-$HOME/.local/bin}/muse}"
binary="$install_dir/muse-bin-$MUSE_VERSION"
url="https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=$MUSE_VERSION&file=$artifact"

mkdir -p "$install_dir"
if [[ ! -f "$binary" ]]; then
  curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --tlsv1.2 -o "$binary.partial" "$url"
  mv "$binary.partial" "$binary"
fi

if ! echo "$sha256  $binary" | sha256sum --check --strict --status; then
  rm -f "$binary"
  echo "::error::Muse $MUSE_VERSION ($artifact) failed sha256 verification" >&2
  exit 1
fi
chmod 0755 "$binary"

export MUSE_NO_AUTO_UPDATE=1
reported="$("$binary" --version)"
if [[ "$reported" != *"($MUSE_VERSION)"* ]]; then
  echo "::error::expected Muse $MUSE_VERSION, binary reports: $reported" >&2
  exit 1
fi
echo "Installed $reported at $binary"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "OMNIGENT_MUSE_PATH=$binary"
    echo "MUSE_NO_AUTO_UPDATE=1"
  } >> "$GITHUB_ENV"
fi
