# EdgeDisco landing page

Standalone static site. No build step, external assets at runtime, analytics, or cookies.

The visual system follows [edgedisco.com](https://edgedisco.com): dark violet canvas, dot grid, lime/cyan/violet signals, luminous globe, and Mona Sans. The locally hosted Mona Sans subset is distributed under the [SIL Open Font License](MONA-SANS-LICENSE.txt).

Preview from the repository root:

```sh
python3 -m http.server 8000 --directory website
```

Open <http://localhost:8000>. Deploy the contents of `website/` to any static host.

The install snippet explicitly selects the canonical `edgedisco/edgedisco` source archive. Keep it aligned with the installer's default if repository hosting changes.
