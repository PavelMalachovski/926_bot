#!/usr/bin/env python3
"""Comprehensive test runner with coverage reporting."""

import subprocess
import sys
import os
from pathlib import Path
from typing import List, Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def run_command(cmd: List[str], description: str) -> bool:
    """Run a command and return success status."""
    print(f"\n🔄 {description}")
    print(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, cwd=project_root, check=True, capture_output=True, text=True)
        print(f"✅ {description} completed successfully")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed")
        if e.stdout:
            print("STDOUT:", e.stdout)
        if e.stderr:
            print("STDERR:", e.stderr)
        return False


def run_tests(
    test_path: Optional[str] = None,
    markers: Optional[str] = None,
    coverage: bool = True,
    parallel: bool = False,
    verbose: bool = True
) -> bool:
    """Run tests with various options."""

    cmd = ["python", "-m", "pytest"]

    if test_path:
        cmd.append(test_path)
    else:
        cmd.append("tests/")

    if markers:
        cmd.extend(["-m", markers])

    if coverage:
        cmd.extend([
            "--cov=app",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "--cov-report=xml:coverage.xml",
            "--cov-fail-under=80"
        ])

    if parallel:
        cmd.extend(["-n", "auto"])

    if verbose:
        cmd.append("-v")

    return run_command(cmd, "Running tests")


def run_linting() -> bool:
    """Run code linting."""
    commands = [
        (["python", "-m", "black", "--check", "app/", "tests/"], "Black formatting check"),
        (["python", "-m", "isort", "--check-only", "app/", "tests/"], "Import sorting check"),
        (["python", "-m", "flake8", "app/", "tests/"], "Flake8 linting"),
        (["python", "-m", "mypy", "app/"], "Type checking with mypy")
    ]

    all_passed = True
    for cmd, description in commands:
        if not run_command(cmd, description):
            all_passed = False

    return all_passed


def run_security_checks() -> bool:
    """Run security checks."""
    commands = [
        (["python", "-m", "bandit", "-r", "app/"], "Security vulnerability scan"),
        (["python", "-m", "safety", "check"], "Dependency vulnerability check")
    ]

    all_passed = True
    for cmd, description in commands:
        if not run_command(cmd, description):
            all_passed = False

    return all_passed


def generate_coverage_report() -> bool:
    """Generate detailed coverage report."""
    return run_command(
        ["python", "-m", "coverage", "html"],
        "Generating HTML coverage report"
    )


def main():
    """Main test runner."""
    import argparse

    parser = argparse.ArgumentParser(description="Comprehensive test runner")
    parser.add_argument("--test-path", help="Specific test path to run")
    parser.add_argument("--markers", help="Pytest markers to filter tests")
    parser.add_argument("--no-coverage", action="store_true", help="Skip coverage reporting")
    parser.add_argument("--parallel", action="store_true", help="Run tests in parallel")
    parser.add_argument("--lint-only", action="store_true", help="Run only linting")
    parser.add_argument("--security-only", action="store_true", help="Run only security checks")
    parser.add_argument("--coverage-only", action="store_true", help="Generate coverage report only")
    parser.add_argument("--unit-only", action="store_true", help="Run only unit tests")
    parser.add_argument("--integration-only", action="store_true", help="Run only integration tests")

    args = parser.parse_args()

    print("🧪 Forex Bot Test Runner")
    print("=" * 50)

    success = True

    if args.coverage_only:
        success = generate_coverage_report()
    elif args.lint_only:
        success = run_linting()
    elif args.security_only:
        success = run_security_checks()
    else:
        # Run tests
        if args.unit_only:
            markers = "unit"
        elif args.integration_only:
            markers = "integration"
        else:
            markers = args.markers

        success = run_tests(
            test_path=args.test_path,
            markers=markers,
            coverage=not args.no_coverage,
            parallel=args.parallel
        )

        # Run additional checks if tests passed
        if success and not args.unit_only and not args.integration_only:
            print("\n🔍 Running additional checks...")

            # Linting
            if not run_linting():
                success = False

            # Security checks
            if not run_security_checks():
                success = False

    print("\n" + "=" * 50)
    if success:
        print("🎉 All tests and checks passed!")
        print("📊 Coverage report available at: htmlcov/index.html")
    else:
        print("❌ Some tests or checks failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
