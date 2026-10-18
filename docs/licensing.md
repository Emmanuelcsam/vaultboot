# Licensing and provenance

Vaultboot is an independent project. It is not affiliated with, sponsored by,
or endorsed by Obsidian or Debian. Obsidian and Debian names belong to their
respective owners. We do not use the Obsidian logo as our distribution logo.

Vaultboot's original scripts and configuration use the repository's MIT license.
This does not relicense Debian packages, firmware, Obsidian, or plugins.

## Obsidian

The public image does not contain the Obsidian application. First setup asks
the user to download it directly from `obsidianmd/obsidian-releases` on GitHub,
the upstream linked by [Obsidian's download page](https://obsidian.md/download).
Its [terms](https://obsidian.md/terms) and
[license overview](https://obsidian.md/license) apply to that application.
The terms do not provide a clear general grant to redistribute its binary
as part of a third-party distribution; this project does not assume one.

The downloader requires a SHA-256 digest in the upstream GitHub asset metadata
and verifies the package before extraction. This detects corrupt downloads;
it does not protect against compromise of the upstream publisher or GitHub.
It never executes the Debian package's maintainer scripts as root.

## Debian and firmware

Debian binaries come from Debian's signed package repositories. The build
includes `main`, `contrib`, and `non-free-firmware` where needed for
hardware support. Package copyright/license notices remain installed under
`/usr/share/doc`, with common licenses under `/usr/share/common-licenses`.

Follow [Debian's CD redistribution requirements](https://www.debian.org/CD/vendors/legal):
publish matching source archives alongside the binary image. A link to a
rolling Debian mirror is not a substitute for retaining the corresponding
source used by a particular release. `live-build` produces the source archive;
the build splits large archives into downloadable release parts.
Keep every source part and its checksum with each binary release.
Firmware packages retain their upstream license terms; some contain
redistributable binary firmware without source.

Review these rules again before each public release; software and terms can change.
