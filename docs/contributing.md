# Contributing

Python 3.11 or later is required; CI exercises 3.11 and 3.12. Pillow 13 security fixes
require Python 3.11+, so Python 3.10 is no longer supported. Docker uses Python 3.11.

Contributions to this project are welcome! If you'd like to contribute, please follow these general guidelines:

1.  **Fork the Repository:**
    Start by forking the main repository to your own GitHub account.

2.  **Create a Branch:**
    Create a new branch in your forked repository for your feature or bug fix. Use a descriptive branch name, e.g., `feat/add-image-filtering` or `fix/upload-error-handling`.
    ```bash
    git checkout -b feat/your-new-feature
    ```

3.  **Make Your Changes:**
    Implement your feature or bug fix.
    *   Ensure your code follows the existing style and structure.
    *   If you add new features, consider adding relevant documentation or examples.
    *   If you add or change a Python dependency in `pyproject.toml`, regenerate the pinned lockfiles. All three are resolved for the Linux platform CI/the Docker image actually install them on via `--python-platform x86_64-unknown-linux-gnu`; omitting it on a non-Linux host can produce hashes that don't match what's actually installable there. Add `--upgrade` to pick up new releases within the ranges already declared in `pyproject.toml` instead of keeping the existing pins.
        *   `uv pip compile pyproject.toml --extra dev --extra server --extra ml --extra onnx --extra capture --generate-hashes --python-platform x86_64-unknown-linux-gnu --python-version 3.11 -o requirements.lock.txt` (used by CI's `pip-audit` job).
        *   `uv pip compile pyproject.toml --extra server --generate-hashes --python-platform x86_64-unknown-linux-gnu --python-version 3.11 -o requirements-server.lock.txt` (used by `Dockerfile` to build the production image — keep this in sync with `requirements.lock.txt` whenever a `server`-extra dependency changes).
        *   `uv pip compile pyproject.toml --extra ml --generate-hashes --python-platform x86_64-unknown-linux-gnu --python-version 3.11 -o requirements-ml.lock.txt` (used by `Dockerfile` when built with `--build-arg PIP_EXTRAS=server,ml` — keep this in sync whenever an `ml`-extra dependency changes).
    *   `static/vendor/swagger-ui/` vendors a specific `swagger-ui-dist` release (currently `5.32.12`) so `GET /api/docs` doesn't depend on a third-party CDN (see #316). To update it, download the new version's tarball (`https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-<version>.tgz`) and replace `swagger-ui.css`, `swagger-ui-bundle.js`, `swagger-ui-standalone-preset.js`, `LICENSE`, and `NOTICE` with the corresponding files from the new release.

4.  **Test Your Changes:**
    Thoroughly test your changes to ensure they work as expected and do not introduce new issues.
    *   If applicable, write unit tests for new functionality.

5.  **Commit Your Changes:**
    Commit your changes with clear and concise commit messages.
    ```bash
    git add .
    git commit -m "feat: Add feature X"
    # or "fix: Resolve bug Y"
    ```

6.  **Push to Your Fork:**
    Push your changes to your forked repository.
    ```bash
    git push origin feat/your-new-feature
    ```

7.  **Open a Pull Request:**
    Go to the original repository and open a Pull Request (PR) from your forked branch to the `main` branch of the original repository.
    *   Provide a clear description of the changes in your PR.
    *   Reference any relevant issues (e.g., "Closes #123").

8.  **Code Review:**
    Your PR will be reviewed, and feedback may be provided. Be prepared to make further changes if requested.

By contributing, you agree that your contributions will be licensed under the project's license.
