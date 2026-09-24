"""Which test framework a repository uses, and whether anything runs its tests.

The test author writes new test files when a change has none. Following an
existing test file tells it the framework; a repository with no tests yet tells
it nothing, and a guessed framework is a test that cannot run. So the answer is
read from the repository's own configuration instead.

Deliberately small and deliberately literal. `detect` is handed two functions -
read a file, list a directory - so the same rules run over a local checkout
while Code Graph indexes it and over the GitHub API when a run has a write
credential. It returns names from closed sets, never file contents: only those
names reach a prompt, so a repository cannot use its own configuration to
instruct the model.
"""

import json

PYTHON = ("pytest", "django", "unittest")
JAVASCRIPT = ("vitest", "jest", "mocha")
JAVA = ("junit5", "junit4", "testng")

#: Where a JVM build declares its test dependencies. Only the root build is
#: read; a multi-module project that declares its test framework in one module
#: alone reads as a Java project with none declared.
JAVA_BUILDS = ("pom.xml", "build.gradle", "build.gradle.kts")

#: What each framework looks like in a build file, checked in this order.
#: spring-boot-starter-test brings JUnit 5, and a build naming both JUnit 4 and
#: Jupiter runs on JUnit 5's platform, so new tests are written for 5.
JAVA_MARKERS = (
    ("testng", ("testng", "usetestng")),
    ("junit5", ("junit-jupiter", "junit.jupiter", "usejunitplatform", "spring-boot-starter-test")),
    ("junit4", ("<artifactid>junit</artifactid>", "junit:junit")),
)

#: The prompt line for each Java framework. JUnit 4 and 5 differ in imports and
#: annotations, and a test written for the wrong one does not compile.
JAVA_LINES = {
    "junit5": "JUnit 5 (Jupiter), which this repository already uses. Import from "
    "org.junit.jupiter.api, annotate tests with @Test",
    "junit4": "JUnit 4, which this repository already uses. Import org.junit.Test "
    "and org.junit.Assert, not JUnit 5's org.junit.jupiter.api",
    "testng": "TestNG, which this repository already uses. Import org.testng.annotations.Test "
    "and org.testng.Assert",
}

#: Places a CI configuration lives. A directory entry means "any file in it".
CI_FILES = (".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile", ".travis.yml")
CI_DIRECTORIES = (".github/workflows", ".circleci")

#: npm's placeholder for a package with no tests.
NO_TEST_SCRIPT = "no test specified"


def detect(read, listdir):
    """{"python", "javascript", "java", "ci", "ci_files", "evidence"} for one repository.

    `read(path)` returns text or None; `listdir(path)` returns entry names or an
    empty list. Either may be asked for paths that do not exist.
    """
    evidence = []
    python = _python(read, listdir, evidence)
    javascript = _javascript(read, evidence)
    java = _java(read, evidence)
    ci_files = [name for name in CI_FILES if read(name) is not None]
    for directory in CI_DIRECTORIES:
        ci_files += [
            f"{directory}/{name}"
            for name in listdir(directory)
            if name.endswith((".yml", ".yaml"))
        ]
    return {
        "python": python,
        "javascript": javascript,
        "java": java,
        "ci": bool(ci_files),
        "ci_files": ci_files[:10],
        "evidence": evidence[:6],
        "known": True,
    }


def _python(read, listdir, evidence):
    pyproject = read("pyproject.toml") or ""
    requirements = "\n".join(
        read(name) or ""
        for name in ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt")
    )
    setup_cfg = read("setup.cfg") or ""
    tox = read("tox.ini") or ""
    declared = f"{pyproject}\n{requirements}".lower()
    if (
        "pytest" in declared
        or read("pytest.ini") is not None
        or read("conftest.py") is not None
        or "[tool:pytest]" in setup_cfg
        or "[pytest]" in tox
    ):
        evidence.append("pytest is configured or declared")
        return "pytest"
    if read("manage.py") is not None and "django" in declared:
        evidence.append("a Django project (manage.py, and Django declared)")
        return "django"
    if pyproject or requirements.strip() or read("setup.py") is not None:
        evidence.append("a Python project with no test runner declared")
        return "unittest"
    return None


def _javascript(read, evidence):
    text = read("package.json")
    if not text:
        return None
    try:
        package = json.loads(text)
    except ValueError:
        return None
    if not isinstance(package, dict):
        return None
    names = set()
    for key in ("dependencies", "devDependencies"):
        section = package.get(key)
        if isinstance(section, dict):
            names |= {str(name) for name in section}
    for framework in JAVASCRIPT:
        if framework in names or (framework == "jest" and "ts-jest" in names):
            evidence.append(f"package.json depends on {framework}")
            return framework
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    script = str(scripts.get("test") or "")
    for framework in JAVASCRIPT:
        if framework in script:
            evidence.append(f"package.json's test script runs {framework}")
            return framework
    if script and NO_TEST_SCRIPT not in script:
        evidence.append("package.json has a test script")
    return None


def _java(read, evidence):
    """The JVM test framework the root build declares, "none", or None.

    "none" is a Java project whose build declares no test framework, which is
    not the same as no Java project: Java has no standard-library test runner,
    so the instruction for it has to say a dependency is missing.
    """
    for name in JAVA_BUILDS:
        text = read(name)
        if text is None:
            continue
        lowered = text.lower().replace(" ", "")
        for framework, markers in JAVA_MARKERS:
            if any(marker in lowered for marker in markers):
                evidence.append(f"{name} declares {framework}")
                return framework
        evidence.append(f"{name} declares no test framework")
        return "none"
    return None


def instruction(setup, language):
    """The line the test author is given about the framework to use.

    Built from the closed sets above, so nothing a repository wrote reaches the
    prompt except which of these names it matched.
    """
    if language not in ("python", "javascript", "java"):
        return (
            "Test framework: follow the existing test files shown, and use the test "
            "framework this repository's build already declares. Add no dependencies."
        )
    # A snapshot recorded before a language was detected has no key for it,
    # which is "not known" - not "none configured".
    if not setup or not setup.get("known") or language not in setup:
        if language == "java":
            return (
                "Test framework: not known for this repository. Follow any existing "
                "test file shown; otherwise write JUnit 5 tests, and do not mix JUnit "
                "versions with tests already in the repository."
            )
        return (
            "Test framework: not known for this repository. Follow any existing test "
            "file shown; otherwise use the language's standard-library test runner "
            "and add no dependencies."
        )
    chosen = setup.get(language)
    if language == "java":
        if chosen in JAVA_LINES:
            return (
                f"Test framework: {JAVA_LINES[chosen]}. Put each test in the same "
                "package as the class it tests, and introduce no other framework or "
                "dependency."
            )
        return (
            "Test framework: this repository's build declares none, and Java has no "
            "standard-library test runner. Write JUnit 5 tests; they will not compile "
            "until the build gains a test-scoped junit-jupiter dependency, so say so "
            "in the change."
        )
    if chosen == "django":
        return (
            "Test framework: Django's test runner. Subclass django.test.TestCase; do "
            "not introduce pytest or any other dependency."
        )
    if chosen:
        return (
            f"Test framework: {chosen}, which this repository already uses. Write "
            "tests for it and do not introduce another framework or dependency."
        )
    runner = "unittest" if language == "python" else "node:test"
    return (
        "Test framework: none is configured in this repository. Use the standard "
        f"library ({runner}) so the file runs without adding a dependency."
    )


def ci_summary(setup):
    """What runs these tests, in words, or "" when that cannot be said."""
    if not setup or not setup.get("known"):
        return ""
    if setup.get("ci"):
        return f"CI configured: {', '.join(setup.get('ci_files') or [])}."
    return "No CI configuration in this repository, so nothing will run these tests."
