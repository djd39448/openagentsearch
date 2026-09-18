"""`python -m openagentsearch.liveness` -- delegates to `build.main()`, the same CLI
`python -m openagentsearch.liveness.build` runs directly."""

from openagentsearch.liveness.build import main

if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    raise SystemExit(main())
