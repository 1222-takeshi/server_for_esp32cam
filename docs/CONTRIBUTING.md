# Contributing Guide

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
    *   If you add new Python dependencies, update `requirements.txt` (if one exists, or consider creating one).

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
