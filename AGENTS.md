## Agent skills

### Issue tracker

Issues use this repository's GitHub Issues and the templates under `.github/ISSUE_TEMPLATE/`. See `docs/agents/issue-tracker.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.

## Commit messages

Use `<type>: <Korean summary>`. Allowed types: `feat`, `fix`, `refactor`, `docs`, `test`, and `chore`.

## 문서 및 주석 작성 규칙

- 사용자가 명시적으로 요청하거나 승인하지 않으면 README 파일을 수정하지 않는다. 기능 구현이나 설정 변경에 따른 문서 보완도 예외로 두지 않는다.
- 코드 주석과 docstring은 한글로 작성한다. 식별자, 라이브러리명 등 필요한 기술 용어는 원문을 유지할 수 있다.

## Testing Guidelines
- Do not add tests automatically for every code change.
- Before writing tests, identify the behavior or regression risk that actually needs protection.
- Prefer a small number of meaningful tests over broad test coverage.
- Prefer integration tests for important application flows.
- Use unit tests mainly for complex logic, edge cases, and pure functions.
- For bug fixes, add one regression test that reproduces the bug before fixing it.
- Do not test implementation details, trivial code, framework behavior, or behavior already covered by existing tests.
- Avoid excessive mocking and duplicate tests.
- During development, run only relevant tests. Run the full test suite before completing the task.
- A test is valuable only if breaking the intended behavior would cause it to fail.

### Workflow

For normal feature work:

`Requirement → Test plan → Implementation → Minimal necessary tests → Verification`

For bug fixes:

`Reproduce with failing test → Fix → Verify test passes`

Before adding multiple tests, briefly explain why each test is necessary and check whether existing tests already cover the behavior.