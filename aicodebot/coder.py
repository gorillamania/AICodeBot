from aicodebot.helpers import exec_and_get_output, logger
from aicodebot.lm import token_size
from pathlib import Path
from pygments.lexers import ClassNotFound, get_lexer_for_mimetype, guess_lexer_for_filename
import fnmatch, mimetypes, re, subprocess


class Coder:
    """
    The Coder class encapsulates the functionality of git, and the local file system.
    """

    UNKNOWN_FILE_TYPE = "unknown"

    @staticmethod
    def auto_file_context(max_tokens, max_file_tokens):
        """Automatically generate a file context based on what we think the user is working on"""
        files_to_include = []
        file_scores = {}

        # To determine the pool of possible files, we start with files that have been recently committed
        possible_files = Coder.git_recent_committed_files()

        # then we add any staged and unstaged files
        possible_files += Coder.git_staged_files()
        possible_files += Coder.git_unstaged_files()

        for file in possible_files:
            # Skip directories and files that don't exist
            if not Path(file).exists() or Path(file).is_dir():
                continue

            # empty files
            file_status = Path(file).stat()
            if file_status.st_size == 0:
                continue

            # Get the modification and access times
            modification_time = file_status.st_mtime
            access_time = file_status.st_atime

            # Skip binary files
            if Coder.is_binary_file(file):
                continue

            # Calculate the score based on the modification and access times
            # For now, we'll just add the two times together, giving a slight preference to modification time
            score = modification_time + (access_time * 0.9)

            # Store the score in the dictionary
            # Store the file without the directory
            file_scores[str(file)] = score

        # Sort the files by score in descending order
        sorted_files = sorted(file_scores, key=file_scores.get, reverse=True)

        # Add files to the list until we reach the max_tokens limit
        for file in sorted_files:
            tokens = token_size(Path(file).read_text())
            if tokens > max_file_tokens:
                continue

            if tokens <= max_tokens:
                files_to_include.append(file)
                max_tokens -= tokens

            if max_tokens <= 0:
                break

        return files_to_include

    @staticmethod
    def clone_repo(repo_url, repo_dir):
        """Clones a git repository from the provided URL to the specified directory.
        If the directory already exists, it updates the repository instead."""
        if Path(repo_dir).exists():
            logger.info(f"Repo {repo_dir} already exists, updating it instead")
            # Reset it first to make sure we don't have any local changes
            subprocess.run(["git", "reset", "--hard"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "pull"], cwd=repo_dir, check=True)
        else:
            logger.info(f"Cloning {repo_url} to {repo_dir}")
            subprocess.run(["git", "clone", repo_url, repo_dir], check=True)

    @classmethod
    def filtered_file_list(cls, path, ignore_patterns=None, use_gitignore=True):
        """Walks through a directory and returns a list of files that are not ignored
        based on the provided ignore patterns and .gitignore files."""
        ignore_patterns = ignore_patterns.copy() if ignore_patterns else []

        base_path = Path(path)

        if use_gitignore:
            # Note: .gitignore files can exist in sub directories as well, such as * in __pycache__ directories
            gitignore_file = base_path / ".gitignore"
            if gitignore_file.exists():
                with gitignore_file.open() as f:
                    ignore_patterns.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))

        out = []
        if base_path.is_dir():
            if not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
                out.append(base_path)
                for item in base_path.iterdir():
                    out += cls.filtered_file_list(item, ignore_patterns, use_gitignore)
        elif not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
            out.append(base_path)

        return out

    @classmethod
    def generate_directory_structure(cls, path, ignore_patterns=None, use_gitignore=True, indent=0):
        """Generate a text representation of the directory structure of a path, used for context for prompts"""
        ignore_patterns = ignore_patterns.copy() if ignore_patterns else []

        base_path = Path(path)

        if use_gitignore:
            # Note: .gitignore files can exist in sub directories as well, such as * in __pycache__ directories
            gitignore_file = base_path / ".gitignore"
            if gitignore_file.exists():
                with gitignore_file.open() as f:
                    ignore_patterns.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))

        structure = ""
        if base_path.is_dir():
            if not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
                structure += "  " * indent + f"- [Directory] {base_path.name}\n"
                for item in base_path.iterdir():
                    structure += cls.generate_directory_structure(item, ignore_patterns, use_gitignore, indent + 1)
        elif not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
            structure += "  " * indent + f"- [File] {base_path.name}\n"

        return structure

    @classmethod
    def get_file_info(cls, file_path):
        """Gets information about a file, including whether it's binary and its file type."""

        mime_type = mimetypes.guess_type(file_path)[0]

        # Use the is_binary_file function to check if the file is binary
        is_binary = cls.is_binary_file(file_path)

        try:
            # Try to get the lexer for the MIME type
            lexer = get_lexer_for_mimetype(mime_type)
        except ClassNotFound:
            try:
                # If that fails, try to guess the lexer based on the file name
                lexer = guess_lexer_for_filename(file_path, "")
            except ClassNotFound:
                # If that also fails, set the file type to UNKNOWN_FILE_TYPE
                file_type = cls.UNKNOWN_FILE_TYPE
            else:
                file_type = lexer.name
        else:
            file_type = lexer.name

        return is_binary, file_type

    @staticmethod
    def git_diff_context(commit=None, files=None):
        """Get a text representation of the git diff for the current commit or staged files, including new files"""
        base_git_diff = ["git", "diff", "-U10"]  # Tell diff to provide 10 lines of context

        if commit:
            # If a commit is provided, just get the diff for that commit
            logger.debug(f"Getting diff for commit {commit}")
            # format=%B is the diff and the commit message
            show = exec_and_get_output(["git", "show", "--format=%B", commit])
            logger.opt(raw=True).debug(f"Diff for commit {commit}: {show}")
            return show
        else:
            # Otherwise, get the diff for the staged files, or if there are none, the diff for the unstaged files
            staged_files = Coder.git_staged_files()
            if staged_files:
                logger.debug(f"Getting diff for staged files: {staged_files}")
                diff_type = "--cached"
            else:
                diff_type = "HEAD"

            file_status = exec_and_get_output(
                ["git", "diff", diff_type, "--name-status"] + list(files or [])
            ).splitlines()

            diffs = []
            for status in file_status:
                status_parts = status.split("\t")
                status_code = status_parts[0][0]  # Get the first character of the status code
                if status_code == "A":
                    # If the file is new, include the entire file content
                    file_name = status_parts[1]
                    if Coder.is_binary_file(file_name):
                        # Don't include the diff for binary files
                        diffs.append(f"## New binary file added: {file_name}")
                    else:
                        diffs.append(f"## New file added: {file_name}")
                        contents = Path(file_name).read_text()
                        diffs.append(contents)
                elif status_code == "R":
                    # If the file is renamed, get the diff and note the old and new names
                    old_file_name, new_file_name = status_parts[1], status_parts[2]
                    diffs.append(f"## File renamed: {old_file_name} -> {new_file_name}")
                    diffs.append(exec_and_get_output(base_git_diff + [diff_type, "--", new_file_name]))
                elif status_code == "D":
                    # If the file is deleted, note the deletion
                    file_name = status_parts[1]
                    diffs.append(f"## File deleted: {file_name}")
                else:
                    # If the file is not new, renamed, or deleted, get the diff
                    file_name = status_parts[1]
                    diffs.append(f"## File changed: {file_name}")
                    diffs.append(exec_and_get_output(base_git_diff + [diff_type, "--", file_name]))

            return "\n".join(diffs)

    @staticmethod
    def git_recent_committed_files(max_files=10, max_commits=3):
        """Get a list of files that have been in the last max_days days."""
        recent_commits = exec_and_get_output(["git", "log", "--format=%H", f"-{max_commits}"]).splitlines()
        if not recent_commits:
            return []
        else:
            # Get the list of files that have been changed in those commits
            out = set()
            for commit in recent_commits:
                out.update(exec_and_get_output(["git", "diff", "--name-only", commit]).splitlines())

            return list(out)[:max_files]

    @staticmethod
    def git_staged_files():
        return exec_and_get_output(["git", "diff", "--cached", "--name-only"]).splitlines()

    @staticmethod
    def git_unstaged_files():
        return exec_and_get_output(["git", "diff", "HEAD", "--name-only"]).splitlines()

    @staticmethod
    def identify_languages(files):
        """Identify the languages of a list of files."""
        languages = set()
        for file in files:
            _, language = Coder.get_file_info(file)
            if language != Coder.UNKNOWN_FILE_TYPE:
                languages.add(language)

        return sorted(list(languages))

    def is_inside_git_repo():
        """Checks if the current directory is inside a git repository."""
        out = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return True
        else:
            logger.debug(f"Not inside a git repo: {out.stderr}")
            return False

    @staticmethod
    def is_binary_file(file_path):
        """Checks if a file is binary or not byt looking for a null byte,
        stopping when you find one or reach the end of the file."""
        chunksize = 4000
        with Path(file_path).open("rb") as file:
            while True:
                chunk = file.read(chunksize)
                if b"\0" in chunk:  # Null byte
                    return True
                if len(chunk) < chunksize:
                    break  # End of file
        return False

    @staticmethod
    def parse_github_url(repo_url):
        """Parse a GitHub URL and return the owner and repo name."""
        pattern = r"(?:https:\/\/github\.com\/|git@github\.com:)([^\/]+)\/([^\/]+?)(?:\.git)?$"
        match = re.match(pattern, repo_url)

        if not match:
            raise ValueError("URL is not a valid GitHub URL")

        owner, repo = match.groups()
        return owner, repo
