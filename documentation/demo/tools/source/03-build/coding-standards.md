# Coding standards — CarePath

| | |
| --- | --- |
| **Document** | BLD-CAREPATH-001 |
| **Version** | 1.1 |
| **Status** | Active |
| **Owner** | Principal Engineer |

House rules, not a style guide. Formatting is `ruff`'s problem; what follows is
the part a linter cannot check.

## Tooling

| Concern | Tool | Gate |
| --- | --- | --- |
| Lint and import order | `ruff check` (E, F, I, B, UP), line length 100 | Blocks merge |
| Tests | `pytest` | Blocks merge |
| Coverage | `coverage`, 85% floor | Blocks merge (NFR-13) |

## Rules

### 1. Every requirement identifier appears in the code that satisfies it

A module, class or function realising a requirement names it in its docstring:
`"""Patient registration. FR-01."""`. The traceability matrix is assembled from
these, so an unnamed requirement is an untraceable one.

### 2. A service never imports a web framework

`carepath.services` and `carepath.domain` must be callable from a batch job.
Status codes live in the error map in `carepath.app` and nowhere else.

### 3. The audit write shares the transaction

Any function reading or writing patient data takes the `Principal` and calls
`audit.record` on the same connection. A service that can be called without a
principal is a service that can read a record anonymously.

### 4. Collections are bounded at the server

Every endpoint returning a list takes a limit **and clamps it** to
`settings.max_page_size`. A limit that is accepted and passed through is not a
limit (NFR-06).

### 5. Refuse, do not repair

Malformed input is rejected with an explanation. Do not silently normalise a
medical record number, a date or a code: the repair makes this service the only
one in the network that accepts that input, and the mismatch surfaces somewhere
else as a duplicate patient.

### 6. Say why, not what

Comments explain decisions and trade-offs. `# increment the counter` is noise;
`# Capping rather than normalising: a patient cannot be more than certain to
return` is the reason a reader needs. Prefer a docstring stating the rule over a
comment restating the code.

### 7. Nothing with a name goes in a log

Redaction catches shaped identifiers — record numbers, telephone numbers, email
addresses. It cannot catch a name, a diagnosis or a free-text referral reason.
Log identifiers, not people (NFR-04).

### 8. Errors are domain types

Raise `NotFound`, `Conflict`, `Invalid`, `ConsentWithheld`. Do not raise
`HTTPException` outside `carepath.api`.

### 9. Tests are named as sentences

`test_a_malformed_number_is_refused_rather_than_corrected`, not `test_mrn_2`. A
failing test should read as the statement that is no longer true.

### 10. A lint suppression carries a reason

`extend-immutable-calls` in `pyproject.toml` lists FastAPI's dependency
constructors and explains why they are not the mistake B008 exists to catch. A
bare `# noqa` is not acceptable in review.

## Review checklist

- [ ] Requirement identifiers in the docstrings
- [ ] Audit event for every access to patient data, in the same transaction
- [ ] Permission declared as a route dependency, not checked in the body
- [ ] Every collection endpoint clamps its limit
- [ ] No PHI in any log call added by this change
- [ ] Domain errors, not HTTP exceptions, outside `carepath.api`
- [ ] Tests named as sentences, and a test for the refusal path
- [ ] Traceability matrix updated if a requirement's coverage changed
