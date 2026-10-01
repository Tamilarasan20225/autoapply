"""
TF-IDF Cosine Similarity Scorer — Stage 1 pre-filter before LLM scoring.

Uses scikit-learn TfidfVectorizer with bigrams to capture tech phrase patterns
like "machine learning", "spring boot", "data pipeline" as single units.

Design rationale:
- Resume + JD share domain vocabulary (tech terms, tool names)
- TF-IDF similarity < 0.12 reliably identifies clearly irrelevant jobs
- Runs in <10ms per job vs ~2s for LLM call
- Expected savings: skip 50-60% of LLM calls, preserving free tier quota
"""

import pickle
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

_DEFAULT_VOCAB_PATH = "data/tfidf_vectorizer.pkl"
# Below this many documents the IDF weights are noise, so triage is disabled.
_MIN_CORPUS_DOCS = 200


class TFIDFScorer:
    """
    Pre-built TF-IDF scorer for comparing job descriptions to a resume.
    Build once, score many.
    """

    def __init__(self, resume_text: str, jd_corpus: list[str] | None = None,
                 vocab_path: str | None = _DEFAULT_VOCAB_PATH):
        """
        Build the TF-IDF vectorizer. The fit is persisted so IDF weights stay
        identical across runs — refitting per batch made the same JD score
        differently depending on which 150 jobs it happened to run with.

        A small corpus produces meaningless IDF, so a fit is only persisted (and
        only reused) once it has seen at least _MIN_CORPUS_DOCS documents.

        Args:
            resume_text: Full resume as plain text
            jd_corpus: Job descriptions used to fit IDF.
            vocab_path: Pickle path for the persisted fit. None disables persistence.
        """
        self._available = False
        self._vectorizer = None
        self._resume_vec = None
        self._resume_text = resume_text
        self._vocab_path = vocab_path
        self._corpus_size = 0

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            self._cosine_similarity = cosine_similarity

            loaded = self._load_vectorizer()
            if loaded is not None:
                self._vectorizer, self._corpus_size = loaded
            else:
                corpus = [resume_text] + (jd_corpus or [])
                self._vectorizer = TfidfVectorizer(
                    ngram_range=(1, 2),
                    stop_words="english",
                    max_features=8000,
                    sublinear_tf=True,
                    min_df=1,
                )
                self._vectorizer.fit(corpus)
                self._corpus_size = len(corpus)
                self._save_vectorizer()

            self._resume_vec = self._vectorizer.transform([resume_text])
            self._available = True

        except ImportError:
            console.print("[yellow]scikit-learn not available — TF-IDF pre-scoring disabled[/yellow]")
        except Exception as e:
            console.print(f"[dim]TFIDFScorer init error: {e}[/dim]")

    @property
    def corpus_size(self) -> int:
        return self._corpus_size

    @property
    def well_fitted(self) -> bool:
        """True when IDF came from enough documents to be trustworthy for triage."""
        return self._available and self._corpus_size >= _MIN_CORPUS_DOCS

    def _load_vectorizer(self):
        if not self._vocab_path or not Path(self._vocab_path).exists():
            return None
        try:
            with open(self._vocab_path, "rb") as f:
                payload = pickle.load(f)
            if not isinstance(payload, dict):
                return None
            if payload.get("resume_fingerprint") != self._fingerprint():
                return None
            return payload["vectorizer"], payload.get("corpus_size", 0)
        except Exception:
            return None

    def _fingerprint(self) -> str:
        import hashlib
        return hashlib.sha256(self._resume_text.encode()).hexdigest()[:16]

    def _save_vectorizer(self) -> None:
        # Don't persist an under-fit vectorizer; it would poison every later run.
        if not self._vocab_path or self._corpus_size < _MIN_CORPUS_DOCS:
            return
        try:
            Path(self._vocab_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._vocab_path, "wb") as f:
                pickle.dump({
                    "vectorizer": self._vectorizer,
                    "corpus_size": self._corpus_size,
                    "resume_fingerprint": self._fingerprint(),
                }, f)
        except Exception as e:
            console.print(f"[dim]TFIDFScorer persist error: {e}[/dim]")

    def refit(self, jd_corpus: list[str]) -> None:
        """Force a refit and re-persist. Only call when the resume itself changed."""
        if not self._available or not jd_corpus:
            return
        try:
            corpus = [self._resume_text] + jd_corpus
            self._vectorizer.fit(corpus)
            self._corpus_size = len(corpus)
            self._resume_vec = self._vectorizer.transform([self._resume_text])
            self._save_vectorizer()
        except Exception as e:
            console.print(f"[dim]TFIDFScorer refit error: {e}[/dim]")

    def score(self, jd_text: str) -> Optional[float]:
        """
        Cosine similarity in [0.0, 1.0], or None when it cannot be measured.
        Returning None (rather than a fabricated 0.5) keeps unmeasured values
        out of the database.
        """
        if not self._available or not jd_text:
            return None

        try:
            jd_vec = self._vectorizer.transform([jd_text])
            sim = self._cosine_similarity(self._resume_vec, jd_vec)[0][0]
            return float(sim)
        except Exception:
            return None

    def batch_score(self, jd_texts: list[str]) -> list[Optional[float]]:
        """
        Score multiple JDs at once (fastest — vectorizes all in one call).

        Args:
            jd_texts: List of job description texts

        Returns:
            List of similarity scores in [0.0, 1.0], or None where unmeasurable.
        """
        if not self._available or not jd_texts:
            return [None] * len(jd_texts)

        try:
            jd_vecs = self._vectorizer.transform(jd_texts)
            sims = self._cosine_similarity(self._resume_vec, jd_vecs)[0]
            return [float(s) for s in sims]
        except Exception:
            return [None] * len(jd_texts)

    @property
    def available(self) -> bool:
        return self._available


def build_resume_text(master_resume: dict) -> str:
    """
    Build a rich plain-text representation of the resume for TF-IDF fitting.
    Includes: all summary variants + skills + experience bullets + project tags.
    """
    parts = []

    # Summary variants
    for variant_text in master_resume.get("summary_variants", {}).values():
        parts.append(variant_text)

    # Skills (all categories)
    for category, items in master_resume.get("skills", {}).items():
        if isinstance(items, list):
            parts.append(" ".join(str(s) for s in items))

    # Experience bullets + project names + tags
    for exp in master_resume.get("experiences", []):
        parts.append(exp.get("role", ""))
        parts.append(exp.get("company", ""))
        for proj in exp.get("projects", []):
            parts.append(proj.get("name", ""))
            for bullet in proj.get("bullets", []):
                parts.append(bullet)
            for tag in proj.get("tags", []):
                parts.append(tag)

    # Education
    for edu in master_resume.get("education", []):
        parts.append(edu.get("degree", ""))

    # Certifications
    for cert in master_resume.get("certifications", []):
        parts.append(cert.get("name", ""))

    return " ".join(p for p in parts if p)


def compute_keyword_overlap(jd_text: str, resume_text: str) -> float:
    """
    Simple bag-of-words keyword overlap ratio.
    Backup when TF-IDF is unavailable.

    Returns: ratio of JD words (>3 chars) found in resume text.
    """
    if not jd_text or not resume_text:
        return 0.0

    import re
    jd_words = set(re.findall(r'\b[a-z]{4,}\b', jd_text.lower()))
    resume_words = set(re.findall(r'\b[a-z]{4,}\b', resume_text.lower()))

    if not jd_words:
        return 0.0

    overlap = jd_words & resume_words
    return len(overlap) / len(jd_words)
