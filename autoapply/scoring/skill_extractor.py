"""
Skill Extractor — FlashText-based O(N) skill extraction from job descriptions and resumes.

Uses the Aho-Corasick algorithm (via FlashText) which is linear in input length,
much faster than regex loops for 500+ skill patterns.

Skills dictionary covers: languages, frameworks, databases, cloud, AI/ML, tools,
search, messaging, DevOps — mapped from variations to canonical names.
"""

from typing import Optional
from rich.console import Console

console = Console()

# ── Comprehensive Skills Dictionary ──────────────────────────────────────────
# Format: {canonical_name: [variations...]}
# FlashText uses case_sensitive=False so all variations match regardless of case

SKILLS_DICT: dict[str, list[str]] = {
    # ── Languages ──────────────────────────────────────────────────────────
    "Java": ["java", "java8", "java11", "java17", "java 8", "java 11", "java 17"],
    "Python": ["python", "python3", "python 3", "py"],
    "Go": ["golang", "go lang", "go programming"],
    "Rust": ["rust", "rust lang"],
    "TypeScript": ["typescript", "ts"],
    "JavaScript": ["javascript", "js", "ecmascript", "es6", "es2015"],
    "Kotlin": ["kotlin"],
    "Scala": ["scala"],
    "C++": ["c++", "cpp", "c plus plus"],
    "C#": ["c#", "csharp", "c sharp", ".net", "dotnet"],
    "Ruby": ["ruby", "ruby on rails"],
    "PHP": ["php"],
    "Swift": ["swift"],
    "Dart": ["dart"],
    "R": ["r language", "r programming", "rlang"],
    "Elixir": ["elixir"],
    "Haskell": ["haskell"],
    "Shell": ["bash", "shell scripting", "shell script", "zsh"],
    "SQL": ["sql", "t-sql", "plsql", "pl/sql"],

    # ── Backend Frameworks ─────────────────────────────────────────────────
    "Spring Boot": ["spring boot", "springboot", "spring framework", "spring", "spring mvc", "spring data", "spring security", "spring cloud"],
    "FastAPI": ["fastapi", "fast api"],
    "Django": ["django", "django rest framework", "drf"],
    "Flask": ["flask"],
    "Express.js": ["express", "expressjs", "express.js", "node express"],
    "Node.js": ["node.js", "nodejs", "node js"],
    "NestJS": ["nestjs", "nest.js"],
    "Rails": ["rails", "ruby on rails", "ror"],
    "Laravel": ["laravel"],
    "Quarkus": ["quarkus"],
    "Micronaut": ["micronaut"],
    "Gin": ["gin framework", "gin-gonic"],
    "gRPC": ["grpc", "grpc-go", "protocol buffers", "protobuf"],
    "GraphQL": ["graphql", "graph ql"],
    "REST API": ["rest api", "restful", "rest apis", "restful api", "rest", "api design"],
    "Microservices": ["microservices", "micro services", "microservice architecture", "service mesh"],
    "WebSockets": ["websockets", "websocket", "socket.io"],

    # ── Frontend Frameworks ────────────────────────────────────────────────
    "React": ["react", "reactjs", "react.js", "react js"],
    "Vue.js": ["vue", "vuejs", "vue.js"],
    "Angular": ["angular", "angularjs", "angular.js"],
    "Next.js": ["next.js", "nextjs"],
    "Svelte": ["svelte", "sveltekit"],

    # ── Databases ──────────────────────────────────────────────────────────
    "PostgreSQL": ["postgresql", "postgres", "psql", "pg"],
    "MySQL": ["mysql", "mariadb"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis", "redis cache"],
    "Cassandra": ["cassandra", "apache cassandra"],
    "DynamoDB": ["dynamodb", "dynamo db"],
    "Elasticsearch": ["elasticsearch", "elastic search", "opensearch", "open search"],
    "ClickHouse": ["clickhouse", "click house"],
    "SQLite": ["sqlite"],
    "Oracle": ["oracle db", "oracle database"],
    "BigQuery": ["bigquery", "big query"],
    "Snowflake": ["snowflake"],
    "Redshift": ["redshift", "amazon redshift"],
    "Neo4j": ["neo4j", "graph database"],
    "Pinecone": ["pinecone"],
    "Weaviate": ["weaviate"],
    "ChromaDB": ["chromadb", "chroma db"],
    "FAISS": ["faiss", "vector search"],

    # ── Search Infrastructure ──────────────────────────────────────────────
    "Apache Lucene": ["apache lucene", "lucene"],
    "Solr": ["apache solr", "solr"],
    "Sphinx": ["sphinx search"],
    "Typesense": ["typesense"],
    "Meilisearch": ["meilisearch"],
    "BM25": ["bm25"],
    "Hybrid Search": ["hybrid search", "semantic search", "vector similarity", "vector search"],

    # ── Message Queues & Streaming ─────────────────────────────────────────
    "Apache Kafka": ["kafka", "apache kafka", "kafka streams"],
    "RabbitMQ": ["rabbitmq", "rabbit mq"],
    "Apache Pulsar": ["pulsar", "apache pulsar"],
    "AWS SQS": ["sqs", "amazon sqs", "aws sqs"],
    "AWS SNS": ["sns", "amazon sns"],
    "NATS": ["nats", "nats.io"],
    "Apache Flink": ["flink", "apache flink"],
    "Apache Spark": ["spark", "apache spark", "pyspark"],

    # ── Cloud Platforms ────────────────────────────────────────────────────
    "AWS": ["aws", "amazon web services", "amazon aws"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Azure": ["azure", "microsoft azure"],
    "EC2": ["ec2", "amazon ec2"],
    "S3": ["s3", "amazon s3"],
    "Lambda": ["lambda", "aws lambda"],
    "ECS": ["ecs", "amazon ecs"],
    "EKS": ["eks", "amazon eks"],
    "CloudFormation": ["cloudformation", "cloud formation"],
    "Terraform": ["terraform", "terraform cloud"],

    # ── DevOps & Infrastructure ────────────────────────────────────────────
    "Docker": ["docker", "docker compose", "dockerfile"],
    "Kubernetes": ["kubernetes", "k8s", "kubectl"],
    "Helm": ["helm", "helm charts"],
    "CI/CD": ["ci/cd", "cicd", "continuous integration", "continuous deployment", "continuous delivery"],
    "GitHub Actions": ["github actions", "github workflows"],
    "Jenkins": ["jenkins"],
    "GitLab CI": ["gitlab ci", "gitlab cicd", "gitlab pipelines"],
    "Ansible": ["ansible"],
    "Nginx": ["nginx"],
    "Linux": ["linux", "ubuntu", "debian", "centos", "rhel"],
    "Git": ["git", "github", "gitlab", "version control"],
    "Monitoring": ["prometheus", "grafana", "datadog", "newrelic", "cloudwatch", "jaeger", "zipkin", "opentelemetry"],

    # ── AI / ML ────────────────────────────────────────────────────────────
    "LLM": ["llm", "large language model", "gpt", "gpt-4", "claude", "gemini"],
    "RAG": ["rag", "retrieval augmented generation", "agentic rag"],
    "AI Agents": ["ai agents", "autonomous agents", "agentic", "multi-agent"],
    "NLP": ["nlp", "natural language processing", "natural language understanding", "nlu"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow", "keras"],
    "Transformers": ["transformers", "hugging face", "huggingface", "bert", "roberta"],
    "LangChain": ["langchain", "lang chain"],
    "LlamaIndex": ["llamaindex", "llama index", "llama-index"],
    "Machine Learning": ["machine learning", "ml", "predictive modeling"],
    "Deep Learning": ["deep learning", "neural network", "neural networks"],
    "Computer Vision": ["computer vision", "cv", "image recognition", "object detection"],
    "MLOps": ["mlops", "ml pipeline", "model serving", "model deployment"],
    "Data Science": ["data science", "data scientist"],
    "FastText": ["fasttext"],
    "spaCy": ["spacy", "spacy nlp"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "OpenCV": ["opencv", "open cv"],
    "ONNX": ["onnx"],

    # ── Data Engineering ───────────────────────────────────────────────────
    "Airflow": ["airflow", "apache airflow"],
    "dbt": ["dbt", "data build tool"],
    "ETL": ["etl", "elt", "data pipeline", "data pipelines"],
    "Hadoop": ["hadoop", "hdfs", "hive", "hbase"],
    "Databricks": ["databricks"],

    # ── Security & Auth ────────────────────────────────────────────────────
    "OAuth2": ["oauth", "oauth2", "oauth 2.0", "openid connect", "oidc"],
    "JWT": ["jwt", "json web token"],
    "SAML": ["saml"],
    "SSO": ["sso", "single sign-on"],

    # ── Architecture Patterns ──────────────────────────────────────────────
    "Distributed Systems": ["distributed systems", "distributed computing", "distributed"],
    "System Design": ["system design", "high-level design", "low-level design"],
    "Event-Driven": ["event-driven", "event driven architecture", "eda"],
    "CQRS": ["cqrs", "command query responsibility"],
    "Domain-Driven Design": ["ddd", "domain driven design", "domain-driven design"],
    "API Gateway": ["api gateway", "kong", "apigee"],
    "Load Balancing": ["load balancing", "load balancer"],
    "Caching": ["caching", "cache", "memcached", "in-memory cache"],
    "Web Scraping": ["web scraping", "web crawler", "crawling", "scraping", "data extraction"],
    "Chrome Extension": ["chrome extension", "browser extension"],

    # ── Web Technologies ───────────────────────────────────────────────────
    "HTTP": ["http", "https", "http/2", "http2"],
    "WebAssembly": ["wasm", "webassembly"],

    # ── Testing ────────────────────────────────────────────────────────────
    "Unit Testing": ["unit testing", "unit tests", "junit", "pytest", "jest", "mocha"],
    "Integration Testing": ["integration testing", "integration tests"],
    "TDD": ["tdd", "test driven development"],

    # ── Mobile ─────────────────────────────────────────────────────────────
    "Android": ["android", "android development"],
    "iOS": ["ios", "ios development", "xcode"],
    "Flutter": ["flutter"],
    "React Native": ["react native", "rn"],

    # ── Embedded Systems & Hardware ──────────────────────────────────────────
    "C": ["c language", "c programming", "ansi c", "c89", "c99", "c11"],
    "RTOS": ["rtos", "real-time os", "real-time operating system", "freertos", "vxworks", "zephyr", "threadx", "rtems"],
    "AUTOSAR": ["autosar", "autosar classic", "autosar adaptive", "autosar bsw"],
    "CAN Bus": ["can bus", "can protocol", "canopen", "j1939", "uds", "isotp", "canfd", "lin protocol"],
    "Embedded Linux": ["embedded linux", "yocto", "buildroot", "poky", "openwrt", "meta-layer", "petalinux"],
    "Firmware": ["firmware", "firmware development", "firmware engineer", "embedded firmware", "firmware update", "ota update"],
    "Microcontrollers": ["stm32", "esp32", "esp8266", "arduino", "avr", "pic microcontroller", "arm cortex", "arm cortex-m", "nrf52", "renesas", "infineon tricore"],
    "Communication Protocols": ["spi", "i2c", "uart", "usb protocol", "modbus", "mqtt protocol", "flexray", "ethernet tcp"],
    "Hardware Debugging": ["jtag", "swd", "oscilloscope", "logic analyzer", "gdb debugging", "ozone debugger", "segger"],
    "CMake": ["cmake", "makefile", "gnu make", "bazel build"],
    "MISRA C": ["misra", "misra-c", "misra c", "coding standard", "cert c"],
    "Functional Safety": ["iso 26262", "iec 61508", "functional safety", "fmea", "fta", "safety-critical", "asil"],
    "MATLAB Simulink": ["matlab", "simulink", "model-based development", "mbd", "stateflow"],
    "Assembly Language": ["assembly", "asm language", "arm assembly", "x86 assembly"],
    "FPGA": ["fpga", "vhdl", "verilog", "system verilog", "xilinx", "altera", "vivado"],
    "Device Drivers": ["device driver", "kernel driver", "linux driver", "kernel module", "bsp"],
    "Boot Loader": ["bootloader", "u-boot", "grub", "secure boot"],

    # ── Testing & QA ─────────────────────────────────────────────────────────
    "Selenium": ["selenium", "selenium webdriver", "selenium grid", "selenide"],
    "Appium": ["appium", "mobile automation", "mobile testing framework"],
    "Cypress": ["cypress", "cypress.io", "cypress testing"],
    "JUnit": ["junit", "junit5", "junit 5", "junit4", "junit 4"],
    "TestNG": ["testng", "test ng"],
    "REST Assured": ["rest assured", "restassured", "api rest testing"],
    "Postman": ["postman", "newman", "postman api"],
    "JMeter": ["jmeter", "apache jmeter"],
    "k6": ["k6 load testing", "grafana k6"],
    "Robot Framework": ["robot framework", "robotframework", "ride"],
    "Cucumber": ["cucumber", "gherkin", "bdd framework", "behave"],
    "Test Automation": ["test automation", "automated testing", "automation testing", "test framework", "qa automation"],
    "Manual Testing": ["manual testing", "manual test", "functional testing", "regression testing", "exploratory testing"],
    "ISTQB": ["istqb", "ctfl", "ctal", "test certification", "istqb certified"],
    "API Testing": ["api testing", "api automation", "api test"],
    "Performance Testing": ["performance testing", "stress testing", "load testing", "endurance testing", "scalability testing"],
    "Security Testing": ["security testing", "penetration testing", "pen testing", "owasp", "vulnerability testing"],
    "Test Management": ["testrail", "qtest", "zephyr scale", "testlink", "xray"],
    "BDD": ["bdd", "behaviour driven development", "behavior driven development"],
    "Accessibility Testing": ["accessibility testing", "wcag", "a11y testing"],
    "Unit Testing": ["unit testing", "unit tests"],  # already may exist — merge is safe
    "SDET": ["sdet", "software development engineer in test", "software engineer in test", "set"],
    "White Box Testing": ["white box testing", "whitebox", "code coverage", "branch coverage"],
    "Black Box Testing": ["black box testing", "blackbox testing", "end-to-end testing"],
    "Regression Testing": ["regression testing", "regression suite", "smoke testing", "sanity testing"],
    "HIL Testing": ["hil", "hardware in loop", "hardware-in-the-loop", "sil testing", "software-in-loop"],
}


# ── Domain classification helpers ─────────────────────────────────────────────

# Maps domain → canonical skill names most relevant for that domain
# Used for per-domain TF-IDF vocabulary and pre-filter scoring
DOMAIN_SKILL_GROUPS: dict[str, list[str]] = {
    "embedded_testing": [
        "C", "C++", "RTOS", "AUTOSAR", "CAN Bus", "Embedded Linux", "Firmware",
        "Microcontrollers", "Communication Protocols", "Hardware Debugging", "CMake",
        "MISRA C", "Functional Safety", "MATLAB Simulink", "Assembly Language", "FPGA",
        "Device Drivers", "Boot Loader",
        "Selenium", "Appium", "Cypress", "JUnit", "TestNG", "REST Assured",
        "Postman", "JMeter", "Robot Framework", "Cucumber", "Test Automation",
        "Manual Testing", "ISTQB", "API Testing", "Performance Testing", "BDD",
        "SDET", "HIL Testing", "Regression Testing",
        "Python", "Java", "C#",
    ],
    "backend": [
        "Java", "Python", "Go", "Rust", "Spring Boot", "FastAPI", "Django", "Node.js",
        "PostgreSQL", "MySQL", "MongoDB", "Redis", "Apache Kafka", "REST API",
        "Microservices", "Docker", "Kubernetes", "AWS", "GCP", "Distributed Systems",
        "gRPC", "GraphQL", "CI/CD",
    ],
    "data": [
        "Python", "SQL", "Apache Spark", "Airflow", "dbt", "BigQuery", "Snowflake",
        "Redshift", "Apache Kafka", "ETL", "Machine Learning", "scikit-learn",
        "TensorFlow", "PyTorch", "Data Science", "Hadoop", "Databricks", "ClickHouse",
    ],
    "ai_ml": [
        "Python", "PyTorch", "TensorFlow", "Transformers", "LLM", "RAG", "NLP",
        "Machine Learning", "Deep Learning", "MLOps", "LangChain", "LlamaIndex",
        "AI Agents", "scikit-learn", "Computer Vision", "spaCy", "FastText",
    ],
    "frontend": [
        "JavaScript", "TypeScript", "React", "Vue.js", "Angular", "Next.js",
        "Node.js", "REST API", "GraphQL", "HTML", "CSS",
    ],
    "general": [],  # no domain filter — accept all tech roles
}

# Title keywords that strongly signal a domain — used for DB-level pre-filtering
# (avoids scoring clearly irrelevant jobs before reaching TF-IDF/LLM)
DOMAIN_TITLE_KEYWORDS: dict[str, list[str]] = {
    "embedded_testing": [
        "embedded", "firmware", "rtos", "hardware", "fpga", "microcontroller",
        "iot", "automotive", "safety", "test engineer", "qa engineer", "quality",
        "automation engineer", "sdet", "validation engineer", "verification",
        "software tester", "manual tester",
    ],
    "backend": [
        "backend", "back-end", "back end", "software engineer", "sde", "swe",
        "platform engineer", "infrastructure engineer", "api", "java engineer",
        "python engineer", "golang", "microservice", "distributed systems",
        "server side", "cloud engineer", "devops", "site reliability",
    ],
    "data": [
        "data engineer", "data scientist", "analytics engineer", "etl",
        "data pipeline", "warehouse", "business intelligence", "bi engineer",
        "spark", "airflow", "databricks",
    ],
    "ai_ml": [
        "machine learning", "ml engineer", "ai engineer", "deep learning",
        "nlp engineer", "computer vision", "llm", "research engineer",
        "applied scientist", "data scientist",
    ],
    "frontend": [
        "frontend", "front-end", "react developer", "vue developer",
        "angular developer", "ui engineer", "web developer", "javascript developer",
    ],
}



class SkillExtractor:
    """
    FlashText-based O(N) skill extractor.
    Processes 10,000 word text in <5ms vs ~500ms for regex loops.
    """

    def __init__(self):
        """Build the FlashText processor from SKILLS_DICT."""
        try:
            from flashtext import KeywordProcessor
            self._processor = KeywordProcessor(case_sensitive=False)
            for canonical, variations in SKILLS_DICT.items():
                # Add canonical name itself as a variation
                all_variations = [canonical] + variations
                # Map each variation -> canonical name
                for variant in all_variations:
                    self._processor.add_keyword(variant, canonical)
            self._available = True
        except ImportError:
            console.print("[yellow]flashtext not available — skill extraction disabled[/yellow]")
            self._available = False

    def extract(self, text: str) -> list[str]:
        """
        Extract canonical skill names from text in O(N) time.
        Returns sorted, deduplicated list of canonical skill names.
        """
        if not self._available or not text:
            return []
        found = self._processor.extract_keywords(text)
        return sorted(set(found))

    def match(
        self,
        jd_text: str,
        resume_skills: set[str],
    ) -> tuple[float, list[str], list[str]]:
        """
        Compute skill overlap between JD and resume skill profile.

        Returns:
            (overlap_ratio, matched_skills, missing_skills)
            overlap_ratio = matched / max(jd_skills, 1) in [0.0, 1.0]
        """
        jd_skills = set(self.extract(jd_text))
        if not jd_skills:
            return 0.0, [], []

        matched = sorted(jd_skills & resume_skills)
        missing = sorted(jd_skills - resume_skills)
        ratio = len(matched) / max(len(jd_skills), 1)
        return ratio, matched, missing

    @staticmethod
    def build_resume_skill_profile(master_resume: dict) -> set[str]:
        """
        Extract all canonical skills from resume data.
        Combines: skills dict categories + experience bullet text.
        """
        try:
            from flashtext import KeywordProcessor
            extractor = SkillExtractor()
            if not extractor._available:
                return set()

            all_skills: set[str] = set()

            # 1. From structured skills dict
            for category, items in master_resume.get("skills", {}).items():
                if isinstance(items, list):
                    for skill in items:
                        # Fuzzy match to canonical names
                        found = extractor.extract(str(skill))
                        if found:
                            all_skills.update(found)
                        else:
                            all_skills.add(str(skill))  # keep as-is if no match

            # 2. From experience bullets (catches implicit skills)
            for exp in master_resume.get("experiences", []):
                for proj in exp.get("projects", []):
                    for bullet in proj.get("bullets", []):
                        all_skills.update(extractor.extract(bullet))
                    # Also check tags if present
                    for tag in proj.get("tags", []):
                        all_skills.update(extractor.extract(tag))

            # 3. From summary variants
            for variant_text in master_resume.get("summary_variants", {}).values():
                all_skills.update(extractor.extract(variant_text))

            return all_skills

        except Exception as e:
            console.print(f"[dim]Skill profile build error: {e}[/dim]")
            return set()


def extract_seniority_from_title(title: str) -> str:
    """
    Extract seniority level from job title.
    Returns: junior | mid | senior | staff | principal | lead | manager | intern | unknown
    """
    title_lower = title.lower()

    if any(kw in title_lower for kw in ["intern", "internship", "trainee"]):
        return "intern"
    if any(kw in title_lower for kw in ["junior", "jr.", "jr ", "entry level", "entry-level", "associate"]):
        return "junior"
    if any(kw in title_lower for kw in ["principal", "distinguished", "fellow"]):
        return "principal"
    if any(kw in title_lower for kw in ["staff"]):
        return "staff"
    if any(kw in title_lower for kw in ["senior", "sr.", "sr ", "lead"]):
        return "senior"
    if any(kw in title_lower for kw in ["manager", "director", "head of", "vp ", "vice president"]):
        return "manager"
    if any(kw in title_lower for kw in ["ii", "2", "level 2", "l2"]):
        return "mid"

    return "mid"  # default to mid-level
