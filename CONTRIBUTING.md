# Contributing to CloudVideoStudio

Thanks for helping shape the project. The repository contains a bounded P0 technical prototype. Its roadmap distinguishes this baseline from future platform features.

## Ways to contribute

- Open an Issue for a reproducible problem, a proposed feature, or a focused technical question.
- Use Discussions for open-ended design and community topics.
- Fork the repository, make a focused change on a branch, and open a Pull Request against `main`.
- Contributions to plugins, documentation, and tests are welcome when their scope and licensing are clear.

For larger or architectural changes, open an Issue or Discussion first so the scope can be agreed before substantial work begins.

## Pull Request guidance

- Explain the problem and the proposed change, and link related Issues.
- Keep changes focused and include tests or verification steps when relevant.
- Do not include credentials, private certificates, unlicensed media, or code copied from another project without authorization.
- Identify third-party code, assets, and dependencies, including their source and license. New dependencies require a license compatibility review before merge.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

The `main` branch accepts changes through Pull Requests. No minimum approval count is required at this early stage so the maintainer can merge reviewed contributions. When CI checks are added, required checks will be enabled before they are treated as merge requirements.

## Development checks

Use Python 3.12+ and run `python -m unittest discover -s tests -v`. Media changes also require `python tools/media_smoke.py --output-dir build/media` with FFmpeg/FFprobe installed. Use a fresh output directory for each run; input media is never overwritten. The P0 GitHub Actions job checks tests, wheel build, real media, OTIO and MLT. Document the results and limitations in your PR.

## Licensing

Original project contributions are made available under the [MIT License](LICENSE). This does not change the license or attribution requirements of third-party components.
