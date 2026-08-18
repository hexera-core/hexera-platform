# Third-party notices

Third-party material redistributed **inside this repository**. (Python and system
dependencies are installed from their own registries at build time under their own
licenses, see `requirements/runtime.txt` and the `Dockerfile`, and are not vendored here.)

## vtk.js, `ui/vendor/vtk.js`

- **Project:** vtk.js (`@kitware/vtk.js`), Kitware Inc. - <https://github.com/Kitware/vtk-js>
- **What is vendored:** a single-file UMD browser bundle of the vtk.js rendering stack,
  used by the in-browser mesh viewer (`ui/js/viewer/viewer.js`). Identification is from the
  bundle itself: `@kitware/vtk.js/...` module paths, Kitware copyright markers, and
  `kitware.github.io/vtk-js` documentation URLs embedded in its strings.
- **Identity of the exact bytes we ship:**

  | | |
  |---|---|
  | SHA-256 | `393d9391b926ade6c59fda061eca0043bccba85e157e2ede5df11607085d8b7a` |
  | Size | 2 349 264 bytes (4 576 lines) |
  | Format | UMD: probes `typeof exports` / `typeof define`, then assigns `window.vtk` and `globalThis.vtk` |
  | Build toolchain | Vite/rollup: content-hashed worker chunks (`PaintFilter.worker-B0DdsMEn.js`, `ComputeHistogram.worker-1Uf8ePg2.js`), a CSS-injection preamble, and a trailing `//# sourceMappingURL=vtk.js.map` |
  | Source map | **not vendored**: the referenced `vtk.js.map` is absent, so the bundle is not debuggable in place |

- **Upstream version: NOT ESTABLISHED, and the reason is that no matching upstream artifact
  exists to establish it against.** This was investigated against primary sources rather than
  assumed; the findings below are what is actually known.

  **1. Kitware publishes no UMD bundle for any version this could be.** The npm registry
  metadata for the `vtk.js` package shows exactly where the UMD build stopped:

  | package era | declared entry point | ships a UMD bundle? |
  |---|---|---|
  | `vtk.js` 1.0.0: 2.1.1 | `./Sources/index.js` | no |
  | `vtk.js` 2.1.2: **17.0.0** | `./dist/vtk.js` | **yes**: the last UMD builds |
  | `vtk.js` 17.0.1: 36.6.0 | `./vtk.js` | no: an ES-module entry |

  `@kitware/vtk.js` (the scoped package, 609 versions) is ES-module-only throughout: its
  root `vtk.js` is a ~1 KB ESM stub, not a bundle. The GitHub releases carry **no** binary
  assets (100 most recent releases checked; zero assets). So for anything past 17.0.0 there
  is no official prebuilt artifact anywhere to compare bytes against.

  **2. These bytes are far newer than 17.0.0, so they cannot be one of those UMD builds.**
  The bundle is a Vite/rollup product (content-hashed worker chunks); the 17.x-era builds
  were webpack. It was therefore **built from source by whoever vendored it**, which is a
  supported thing to do with vtk.js, and leaves no published artifact behind.

  **3. The source version it was built from is bracketed to between `36.2.1` and `36.2.2`.** 758 long,
  distinctive string literals were extracted from the bundle and tested for presence in the
  published sources of 25 releases spanning 17.0.0 → 36.6.0. The signal is unambiguous:

  | vtk.js source | literals present |
  |---|---|
  | 17.0.0 | 325 / 758 |
  | 27.0.0 | 566 / 758 |
  | 35.0.0 | 629 / 758 |
  | 36.0.0 | 738 / 758 |
  | **36.2.1** | **758 / 758** |
  | **36.2.2** | **758 / 758** |
  | 36.2.3 | 751 / 758 |
  | 36.6.0 | 744 / 758 |

  This identifies the **source lineage**, not the artifact. It is evidence about which
  sources the bundle was built from; it is not a byte-level provenance proof and is not
  presented as one. Two adjacent releases fit equally well, and a content match cannot rule
  out local modification before or after the build.

  **What remains open:** whether these exact bytes are an unmodified build of unmodified
  `36.2.1`/`36.2.2` sources. That cannot be settled by comparison because there is nothing
  official to compare with.

  **Why it was not "fixed" by replacing the file.** The only replacement that would carry
  real provenance is a bundle we build ourselves from a pinned upstream tag, which means
  adding npm, Vite and a lockfile to a frontend that deliberately has no build step, to
  serve one vendored file. The alternative, downgrading to the last official UMD build
  (17.0.0, ~2020, nineteen major versions back), is a substantial functional regression to
  the mesh viewer in exchange for paperwork. Neither trade is worth making for a bundle that
  is byte-pinned, integrity-checked in CI, and working. **The provenance gap is documented,
  not closed.**

  **How to close it,** whenever the viewer is next changed for a real reason: build the
  bundle from a pinned `@kitware/vtk.js` tag in a throwaway container, record the tag, the
  exact build command and the resulting checksum here, and update `ui/vendor/SHA256SUMS`.
  That makes the artifact reproducible from source, which is the only form of provenance
  available for a build product.

- **Verification**: the pinned bytes are machine-checkable, in the tree and in the image:

  ```bash
  cd ui/vendor && sha256sum -c SHA256SUMS      # -> vtk.js: OK
  ```

  `ui/vendor/SHA256SUMS` is the pin. Gate C runs the same command **inside the built
  application image**, so a release fails if what ships is not the audited bundle. Updating
  the vendored file therefore requires updating the pin, which is the point: it forces this
  note to be revisited rather than silently drifting.

  Note that this is a release-artifact check, not a unit test. Asserting a vendored
  dependency's bytes from the dev test suite would fail on every legitimate upgrade and
  teach the next reader to delete the assertion.

- **License:** BSD 3-Clause (below), reproduced from the upstream `LICENSE` file as
  required for redistribution in binary/bundled form.

```
Copyright (c) 2016, Kitware Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

The upstream VTK C++ toolkit's own notices (e.g. "Copyright (c) Ken Martin, Will
Schroeder, Bill Lorensen") appear inside the bundle where vtk.js carries them; they are
retained verbatim in the vendored file.
