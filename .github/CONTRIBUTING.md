# 📘 CONTRIBUTING.md — Guide to contributing to CERCAPP

Welcome to Spotifysaver. This document establishes some conventions and good practices for contributing to the project in an organized and collaborative way. Suggestions and improvements are welcome!

---

### 🧰 Requirements
Before you begin, make sure you have installed:

- Python (> 3.9)
- Poetry (https://python-poetry.org/)
- Git (https://git-scm.com/)
- An editor like VS Code (it is recommended to use the shared configuration in `.vscode/`)

---

### 🌱 Basic workflow

1. **Clone the repository**:
   ```bash
   git clone git@github.com:gabrielbaute/spotify-saver.git
   cd spotify-saver
   ```

2. **Create a branch for your contribution**:
Use a clear convention:
   - `ft-feature-name` → new feature
   - `fix-bug-name` → bug fix
   - `refactor-module-name` → code improvement
   - `doc-section-name` → documentation

Example:
```bash
git checkout -b ft-fix-single-track-download-error
```

3. **Make clear and atomic commits**:
   - Use the present tense: `Add Spotify link validation`
   - Avoid generic commits like `miscellaneous changes`

4. **Sync with `main` before doing PR**:
   ```bash
   git fetch origin
   git rebase origin/main
   ```

5. **Open a Pull Request**:
   - Use the automatic template
   - Match the PR to the corresponding issue (if applies)
   - Make sure you pass the tests and validations (if applies)

---

### 🧪 Validations and Testing

Before submitting your PR:

- Verify that the current flow is not broken
- If you modify critical logic, document the changes
- If you add features, consider writing tests (although we don't currently use unit tests, we plan to move towards testability)

---

### 🧭 Good Practices

- Maintain modularity: separate logic, validations, and views
- Document new features and services
- Use enums or constants for roles, states, and permissions
- Avoid unnecessary coupling between frontend and backend
- If you refactor, explain why in the pull request

---

### 📌 Useful links

- [Project README](./README.md)
- [Issue templates](.github/ISSUE_TEMPLATE/)
- [Pull request template](.github/PULL_REQUEST_TEMPLATE.md)