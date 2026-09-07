The Bello worker uses Pi 0.85.1 packages from https://github.com/earendil-works/pi,
licensed under the MIT license in LICENSE-Pi. The worker is independently
implemented integration code; Bello is not a fork of Pi.

The pinned npm lockfile identifies transitive dependencies and their licenses.
Those packages are installed as dependencies, not copied into Bello's Python
wheel. Their own notices remain in the installed packages.

The optional Claude Code backend uses Anthropic's official claude-agent-sdk
package and its bundled, unmodified Claude Code CLI. The SDK and CLI retain
their respective terms and notices; they are not relicensed by Bello.
