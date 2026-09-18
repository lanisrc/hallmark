# Hallmark Contribution Protocol

## Overall GitHub Protocol

**Summer Intern Protocol:** Create your own forked repository for your development, then clone it to your local machine. When working on a new feature, create a new branch. For collaboration on a branch with other interns, merge across your forks by creating a pull request that they review and accept. When a feature collaboration is done, merge that branch into the `main` branch in your forked repository, then one intern makes a PR to the original repository. All interns should periodically pull from the original repository to keep their fork up to date. Contacting peers every week to merge changes and update personal forks helps keep branches synchronized so conflicts can be resolved and work is not duplicated.

**Academic Year Protocol:** Create a new branch whenever there is a new feature or fix to implement. Each branch should focus on a single feature or fix that no existing branch is already addressing. Work in that branch until the feature or fix is complete. Then open a pull request to merge the branch into `main`. Once approved, merge the branch and delete it.

**General Note:** A pull request should be treated as a "super-commit" that is self-contained. It should update the code from one working state to another working state without leaving the project in a non-functional state. As part of every PR, update the README and documentation to reflect any relevant code changes.

## Documentation Protocol


* Update the README after a PR if there have been significant changes to functionality.
* When creating a new class, function, or file, include appropriate docstrings.
* If there are two ways to implement something and you choose one approach, leave an inline comment with a brief justification.
* Try to add comments to code that would be difficult to understand from reading it alone.

## Naming Protocol


* Keep branch names short but descriptive so others can easily tell what you are working on without making the name cumbersome to type.
* Name classes, functions, and files using the existing naming conventions in the project.
* Avoid overly general names that could be misunderstood or confused with something else.

## Testing Protocol


* If the implementation of a feature or fix is less clear than the desired outcome, write unit or end-to-end tests first to define the expected functionality. Then write the code to make those tests pass.
* If the implementation is as clear as (or clearer than) the goal, implement the solution first. Then write tests to ensure the solution covers edge cases and realistic user scenarios. The tests may reveal places where the implementation needs to be fixed, expanded, or simplified.
* Run `pytest --cov` to see which lines the tests miss, or `pytest --cov --cov-report=html` for a browsable report in `htmlcov/`. CI combines coverage from every test job, comments on each pull request with the coverage of its changed lines, and fails if the total drops below the floor set in `.github/workflows/ci.yml`.

## Data Management


* Naming conventions are not always uniform, so matching files to a given `fmt` can be difficult when they do not follow the same convention. Although there are fallback mechanisms, some files may still go unmatched. Keep track of unmatched files to ensure no data is lost.
* EHT datasets can be very large, so be mindful of efficiency when reading, processing, and storing data.
