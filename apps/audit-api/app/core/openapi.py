from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


API_DESCRIPTION = """
API-ul ProofGuard coordonează proiecte de audit pentru contracte inteligente, noduri
specializate, subneturi, finding-uri, reproducerea vulnerabilităților, validarea
rezultatelor și distribuirea recompenselor.

Fluxul principal este:

1. creează și pregătește un proiect de audit;
2. înregistrează nodurile și subneturile care participă la audit;
3. trimite finding-urile descoperite și calculează contribuțiile;
4. reproduce și validează finding-urile;
5. generează raportul final și procesează reputația și recompensele.

Toate răspunsurile și corpurile cererilor sunt validate folosind schemele afișate
în secțiunea **Schemas**. Endpointurile care modifică starea pot întoarce `400`
pentru date invalide, `404` pentru resurse inexistente și `409` pentru conflicte
de stare sau operații duplicate.
""".strip()


OPENAPI_TAGS = [
    {"name": "health", "description": "Verificarea disponibilității serviciului."},
    {"name": "projects", "description": "Crearea, inspectarea și pregătirea proiectelor de audit."},
    {"name": "nodes", "description": "Registrul nodurilor care participă în rețeaua ProofGuard."},
    {"name": "subnets", "description": "Administrarea subneturilor specializate pe categorii de vulnerabilități."},
    {"name": "subnet-membership", "description": "Evaluarea și administrarea apartenenței nodurilor la subneturi."},
    {"name": "routing", "description": "Calcularea și consultarea rutării unui audit către subneturi și noduri."},
    {"name": "submissions", "description": "Trimiterea finding-urilor descoperite de noduri către protocol."},
    {"name": "contributions", "description": "Calcularea contribuției și eligibilității pentru recompensă."},
    {"name": "reproduction", "description": "Încărcarea și executarea controlată a demonstrațiilor Proof of Concept."},
    {"name": "validation", "description": "Validarea finding-urilor și stocarea deciziilor de validare."},
    {"name": "reports", "description": "Generarea și citirea raportului final de audit."},
    {"name": "category-performance", "description": "Metrici brute de performanță ale nodurilor pentru fiecare categorie."},
    {"name": "category-scores", "description": "Scoruri normalizate ale nodurilor pentru fiecare categorie."},
    {"name": "reputation", "description": "Reputația nodurilor și istoricul evenimentelor care o modifică."},
    {"name": "rewards", "description": "Cicluri de recompense, plăți și penalizări la nivel de nod."},
    {"name": "subnet-rewards", "description": "Alocarea recompenselor între subneturile și nodurile rutate."},
]


# Cheia este (metoda HTTP, calea completă), exact cum apare în schema OpenAPI.
OPERATION_DOCS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/health"): (
        "Verifică starea serviciului",
        "Confirmă că API-ul este pornit și poate răspunde la cereri.",
    ),

    # Nodes
    ("POST", "/nodes"): (
        "Înregistrează un nod",
        "Creează un nod nou în registrul protocolului și returnează înregistrarea persistentă.",
    ),
    ("GET", "/nodes"): (
        "Listează nodurile",
        "Returnează nodurile înregistrate, cu filtrare opțională după tip, stare, categorie și operator.",
    ),
    ("GET", "/nodes/{node_id}"): (
        "Citește un nod",
        "Returnează datele nodului identificat prin `node_id`.",
    ),
    ("PATCH", "/nodes/{node_id}"): (
        "Actualizează un nod",
        "Modifică parțial proprietățile editabile ale unui nod existent.",
    ),
    ("POST", "/nodes/{node_id}/status"): (
        "Schimbă starea unui nod",
        "Aplică o tranziție validă de stare pentru nodul specificat.",
    ),

    # Subnets
    ("POST", "/subnets"): (
        "Creează un subnet",
        "Înregistrează un subnet specializat pentru o categorie de vulnerabilități.",
    ),
    ("POST", "/subnets/bootstrap"): (
        "Inițializează subneturile implicite",
        "Creează sau reutilizează setul standard de subneturi pentru categoriile suportate.",
    ),
    ("GET", "/subnets"): (
        "Listează subneturile",
        "Returnează subneturile, cu filtrare opțională după categorie și stare.",
    ),
    ("GET", "/subnets/by-category/{category}"): (
        "Găsește subnetul unei categorii",
        "Returnează subnetul înregistrat pentru categoria de vulnerabilitate indicată.",
    ),
    ("GET", "/subnets/{subnet_id}"): (
        "Citește un subnet",
        "Returnează configurația și starea subnetului identificat prin `subnet_id`.",
    ),
    ("PATCH", "/subnets/{subnet_id}"): (
        "Actualizează un subnet",
        "Modifică parțial configurația editabilă a unui subnet ne-arhivat.",
    ),
    ("POST", "/subnets/{subnet_id}/status"): (
        "Schimbă starea unui subnet",
        "Aplică o tranziție validă de stare pentru subnetul specificat.",
    ),
    ("GET", "/subnets/{subnet_id}/members"): (
        "Listează membrii unui subnet",
        "Returnează membrii subnetului, cu filtre opționale pentru stare, scor și numărul de submission-uri finalizate.",
    ),
    ("GET", "/subnets/{subnet_id}/members/{node_id}"): (
        "Citește apartenența unui nod",
        "Returnează înregistrarea care descrie apartenența nodului la subnet.",
    ),

    # Subnet membership
    ("POST", "/subnets/{subnet_id}/members/refresh"): (
        "Reevaluează membrii subnetului",
        "Recalculează eligibilitatea nodurilor pentru subnet și actualizează înregistrările de apartenență.",
    ),
    ("POST", "/subnets/{subnet_id}/members/{node_id}/evaluate"): (
        "Evaluează un nod pentru subnet",
        "Verifică scorul, performanța și condițiile de eligibilitate ale unui singur nod pentru subnet.",
    ),
    ("GET", "/subnets/{subnet_id}/members/{node_id}/history"): (
        "Citește istoricul apartenenței",
        "Returnează evenimentele de evaluare și schimbare a apartenenței nodului la subnet.",
    ),
    ("POST", "/subnets/{subnet_id}/members/{node_id}/suspend"): (
        "Suspendă un membru",
        "Suspendă temporar nodul din subnet și înregistrează motivul operației.",
    ),
    ("POST", "/subnets/{subnet_id}/members/{node_id}/remove"): (
        "Elimină un membru",
        "Elimină nodul din subnet și păstrează evenimentul în istoricul apartenenței.",
    ),

    # Category performance
    ("POST", "/nodes/{node_id}/category-performance/rebuild"): (
        "Recalculează performanța unui nod",
        "Reconstruiește metricile de performanță pentru toate categoriile asociate nodului.",
    ),
    ("POST", "/nodes/{node_id}/category-performance/{category}/rebuild"): (
        "Recalculează performanța unei categorii",
        "Reconstruiește metricile de performanță ale nodului pentru categoria indicată.",
    ),
    ("POST", "/category-performance/rebuild"): (
        "Recalculează toate performanțele",
        "Reconstruiește în lot metricile de performanță pentru toate nodurile și categoriile disponibile.",
    ),
    ("GET", "/nodes/{node_id}/category-performance"): (
        "Listează performanțele unui nod",
        "Returnează toate înregistrările de performanță pe categorii ale nodului.",
    ),
    ("GET", "/nodes/{node_id}/category-performance/{category}"): (
        "Citește performanța unei categorii",
        "Returnează metricile de performanță ale nodului pentru categoria indicată.",
    ),
    ("GET", "/category-performance"): (
        "Listează performanțele globale",
        "Returnează înregistrările de performanță, filtrabile după nod, categorie și număr minim de submission-uri finalizate.",
    ),
    ("GET", "/subnets/{subnet_id}/category-performance"): (
        "Listează performanțele unui subnet",
        "Returnează performanțele pe categoria subnetului pentru toate nodurile sale.",
    ),

    # Category scores
    ("POST", "/nodes/{node_id}/category-scores/rebuild"): (
        "Recalculează scorurile unui nod",
        "Reconstruiește scorurile tuturor categoriilor nodului; opțional reconstruiește mai întâi performanța sursă.",
    ),
    ("POST", "/nodes/{node_id}/category-scores/{category}/rebuild"): (
        "Recalculează scorul unei categorii",
        "Reconstruiește scorul nodului pentru categoria indicată din metricile sale de performanță.",
    ),
    ("POST", "/category-scores/rebuild"): (
        "Recalculează toate scorurile",
        "Reconstruiește în lot scorurile pe categorii pentru toate nodurile disponibile.",
    ),
    ("GET", "/nodes/{node_id}/category-scores"): (
        "Listează scorurile unui nod",
        "Returnează scorurile normalizate ale nodului pentru toate categoriile sale.",
    ),
    ("GET", "/nodes/{node_id}/category-scores/{category}"): (
        "Citește scorul unei categorii",
        "Returnează scorul și nivelul de încredere al nodului pentru categoria indicată.",
    ),
    ("GET", "/category-scores"): (
        "Listează scorurile globale",
        "Returnează scorurile pe categorii, cu filtre pentru nod, categorie, scor minim, încredere și bandă de scor.",
    ),
    ("GET", "/subnets/{subnet_id}/category-scores"): (
        "Listează scorurile unui subnet",
        "Returnează scorurile relevante pentru nodurile asociate subnetului.",
    ),

    # Submissions
    ("POST", "/projects/{project_id}/submissions"): (
        "Trimite un finding",
        "Înregistrează submission-ul prin care un nod asociază unui proiect un finding existent și o rutare validă.",
    ),
    ("GET", "/projects/{project_id}/submissions"): (
        "Listează submission-urile proiectului",
        "Returnează submission-urile proiectului, filtrabile după nod, stare, categorie și starea recompensei.",
    ),
    ("GET", "/submissions/{submission_id}"): (
        "Citește un submission",
        "Returnează submission-ul identificat prin `submission_id`.",
    ),
    ("GET", "/nodes/{node_id}/submissions"): (
        "Listează submission-urile unui nod",
        "Returnează submission-urile create de nod, cu filtre opționale pentru proiect și stare.",
    ),

    # Contributions
    ("POST", "/submissions/{submission_id}/contribution/calculate"): (
        "Calculează contribuția unui submission",
        "Evaluează finding-ul și validarea asociată pentru a calcula scorul și eligibilitatea contribuției.",
    ),
    ("GET", "/submissions/{submission_id}/contribution"): (
        "Citește contribuția unui submission",
        "Returnează scorul de contribuție calculat pentru submission-ul indicat.",
    ),
    ("GET", "/projects/{project_id}/contributions"): (
        "Listează contribuțiile proiectului",
        "Returnează contribuțiile proiectului, filtrabile după nod și eligibilitatea pentru recompensă.",
    ),
    ("GET", "/nodes/{node_id}/contributions"): (
        "Listează contribuțiile unui nod",
        "Returnează contribuțiile nodului, filtrabile după proiect și eligibilitatea pentru recompensă.",
    ),

    # Reputation
    ("POST", "/submissions/{submission_id}/reputation/process"): (
        "Procesează reputația unui submission",
        "Aplică asupra reputației nodului evenimentul rezultat din contribuția și validarea submission-ului.",
    ),
    ("GET", "/nodes/{node_id}/reputation"): (
        "Citește reputația unui nod",
        "Returnează reputația curentă și sumarul evenimentelor pentru nod.",
    ),
    ("GET", "/nodes/{node_id}/reputation/history"): (
        "Citește istoricul reputației",
        "Returnează evenimentele de reputație ale nodului, cu filtre opționale pentru tip și stare de aplicare.",
    ),
    ("GET", "/reputation/events/{event_id}"): (
        "Citește un eveniment de reputație",
        "Returnează detaliile evenimentului identificat prin `event_id`.",
    ),
    ("GET", "/projects/{project_id}/reputation/events"): (
        "Listează evenimentele de reputație ale proiectului",
        "Returnează evenimentele generate în cadrul proiectului, filtrabile după nod, tip și stare.",
    ),

    # Node rewards and penalties
    ("POST", "/projects/{project_id}/reward-cycles"): (
        "Creează un ciclu de recompense",
        "Creează un ciclu draft pentru distribuirea recompenselor aferente unui proiect.",
    ),
    ("POST", "/reward-cycles/{cycle_id}/calculate"): (
        "Calculează un ciclu de recompense",
        "Calculează alocările pe baza submission-urilor și contribuțiilor eligibile din ciclu.",
    ),
    ("POST", "/reward-cycles/{cycle_id}/finalize"): (
        "Finalizează un ciclu de recompense",
        "Blochează rezultatul calculat și emite evenimentele finale de recompensă.",
    ),
    ("GET", "/reward-cycles/{cycle_id}"): (
        "Citește un ciclu de recompense",
        "Returnează configurația, starea și totalurile ciclului indicat.",
    ),
    ("GET", "/projects/{project_id}/reward-cycles"): (
        "Listează ciclurile de recompense ale proiectului",
        "Returnează toate ciclurile de recompense asociate proiectului.",
    ),
    ("GET", "/submissions/{submission_id}/reward"): (
        "Citește recompensa unui submission",
        "Returnează evenimentul de recompensă asociat submission-ului.",
    ),
    ("GET", "/nodes/{node_id}/rewards"): (
        "Citește recompensele unui nod",
        "Returnează sumarul recompenselor nodului și evenimentele care compun totalul.",
    ),
    ("POST", "/submissions/{submission_id}/penalty/process"): (
        "Procesează penalizarea unui submission",
        "Evaluează un submission nesigur și creează, dacă este cazul, evenimentul de penalizare al nodului.",
    ),
    ("GET", "/submissions/{submission_id}/penalty"): (
        "Citește penalizarea unui submission",
        "Returnează evenimentul de penalizare asociat submission-ului.",
    ),
    ("GET", "/nodes/{node_id}/penalties"): (
        "Listează penalizările unui nod",
        "Returnează toate evenimentele de penalizare aplicate nodului.",
    ),

    # Projects
    ("POST", "/projects"): (
        "Creează un proiect de audit",
        "Încarcă manifestul de scope și sursele proiectului dintr-o arhivă ZIP sau dintr-un repository GitHub.",
    ),
    ("GET", "/projects/{project_id}"): (
        "Citește proiectul de audit",
        "Returnează metadatele proiectului identificat prin `project_id`.",
    ),
    ("GET", "/projects/{project_id}/scope"): (
        "Citește scope-ul proiectului",
        "Returnează manifestul validat care definește contractele și regulile auditului.",
    ),
    ("GET", "/projects/{project_id}/status"): (
        "Citește starea proiectului",
        "Returnează starea curentă a proiectului și ultima eroare de pregătire, dacă există.",
    ),
    ("POST", "/projects/{project_id}/prepare"): (
        "Pornește pregătirea proiectului",
        "Lansează în fundal verificarea repository-ului și a contractelor declarate în scope.",
    ),

    # Routing
    ("POST", "/projects/{project_id}/routing/calculate"): (
        "Calculează rutarea proiectului",
        "Selectează subneturile și nodurile eligibile pentru categoriile cerute de proiect și creează un plan draft.",
    ),
    ("POST", "/projects/{project_id}/routing/{routing_id}/finalize"): (
        "Finalizează planul de rutare",
        "Validează sursele planului și fixează selecția finală de subneturi și noduri.",
    ),
    ("GET", "/projects/{project_id}/routing/latest"): (
        "Citește cea mai recentă rutare",
        "Returnează ultimul plan de rutare creat pentru proiect.",
    ),
    ("GET", "/projects/{project_id}/routing/{routing_id}"): (
        "Citește un plan de rutare",
        "Returnează planul de rutare identificat prin `routing_id` pentru proiect.",
    ),
    ("GET", "/projects/{project_id}/routing"): (
        "Listează rutările proiectului",
        "Returnează istoricul planurilor de rutare ale proiectului.",
    ),
    ("GET", "/nodes/{node_id}/routing-usage"): (
        "Listează utilizarea unui nod în rutări",
        "Returnează planurile de rutare în care a fost selectat nodul.",
    ),
    ("GET", "/subnets/{subnet_id}/routing-usage"): (
        "Listează utilizarea unui subnet în rutări",
        "Returnează planurile de rutare în care a fost selectat subnetul.",
    ),

    # Subnet rewards
    ("POST", "/projects/{project_id}/subnet-reward-cycles"): (
        "Creează un ciclu de recompense pentru subneturi",
        "Creează sau reutilizează ciclul draft asociat unei rutări finalizate a proiectului.",
    ),
    ("POST", "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}/calculate"): (
        "Calculează recompensele subneturilor",
        "Calculează alocările pe categorii, subneturi și noduri pentru ciclul specificat.",
    ),
    ("POST", "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}/finalize"): (
        "Finalizează recompensele subneturilor",
        "Verifică invariantul de conservare și emite evenimentele finale de recompensă.",
    ),
    ("GET", "/projects/{project_id}/subnet-reward-cycles/{reward_cycle_id}"): (
        "Citește ciclul de recompense al subneturilor",
        "Returnează ciclul de recompense identificat în cadrul proiectului.",
    ),
    ("GET", "/projects/{project_id}/subnet-reward-cycles"): (
        "Listează ciclurile de recompense ale subneturilor",
        "Returnează ciclurile proiectului, cu filtrare opțională după stare.",
    ),
    ("GET", "/projects/{project_id}/routing/{routing_id}/subnet-reward-cycle"): (
        "Găsește recompensa unei rutări",
        "Returnează ciclul de recompense pentru subneturi asociat planului de rutare.",
    ),
    ("GET", "/nodes/{node_id}/subnet-rewards"): (
        "Listează recompensele de subnet ale unui nod",
        "Returnează evenimentele nodului, filtrabile după proiect, categorie și subnet.",
    ),
    ("GET", "/subnets/{subnet_id}/reward-events"): (
        "Listează recompensele unui subnet",
        "Returnează evenimentele de recompensă ale subnetului, filtrabile după proiect și nod.",
    ),
    ("GET", "/submissions/{submission_id}/subnet-rewards"): (
        "Listează recompensele unui submission",
        "Returnează evenimentele de recompensă pentru subneturi asociate submission-ului.",
    ),

    # Reproduction
    ("POST", "/projects/{project_id}/findings/{finding_id}/poc"): (
        "Încarcă un Proof of Concept",
        "Validează și stochează fișierul PoC pentru finding, apoi marchează reproducerea ca generată.",
    ),
    ("POST", "/projects/{project_id}/findings/{finding_id}/reproduction/run"): (
        "Rulează reproducerea unui finding",
        "Execută verificările de siguranță și testul Foundry într-un sandbox izolat, fără acces la rețea.",
    ),
    ("GET", "/projects/{project_id}/findings/{finding_id}/reproduction"): (
        "Citește reproducerea unui finding",
        "Returnează ultima stare și rezultatele reproducerii pentru finding.",
    ),
    ("GET", "/projects/{project_id}/reproductions"): (
        "Listează reproducerile proiectului",
        "Returnează rezultatele reproducerilor, cu filtrare opțională după stare.",
    ),

    # Validation
    ("POST", "/projects/{project_id}/findings/{finding_id}/validate"): (
        "Validează un finding",
        "Evaluează scope-ul, reproducerea, duplicatele și nivelul de încredere și salvează decizia.",
    ),
    ("GET", "/projects/{project_id}/findings/{finding_id}/validation"): (
        "Citește validarea unui finding",
        "Returnează decizia de validare salvată pentru finding.",
    ),
    ("GET", "/projects/{project_id}/validations"): (
        "Listează validările proiectului",
        "Returnează toate deciziile de validare salvate în proiect.",
    ),
    ("POST", "/projects/{project_id}/validate-all"): (
        "Validează toate finding-urile",
        "Rulează pipeline-ul de validare pentru toate finding-urile proiectului și returnează deciziile.",
    ),

    # Reports
    ("POST", "/projects/{project_id}/reports/final"): (
        "Generează raportul final",
        "Construiește și salvează versiunile JSON și Markdown ale raportului din finding-urile validate.",
    ),
    ("GET", "/projects/{project_id}/reports/final"): (
        "Citește raportul final JSON",
        "Returnează forma structurată JSON a raportului final generat.",
    ),
    ("GET", "/projects/{project_id}/reports/final/markdown"): (
        "Citește raportul final Markdown",
        "Returnează ca text versiunea Markdown a raportului final generat.",
    ),
}


def configure_openapi(app: FastAPI) -> None:
    """Completează schema generată de FastAPI cu documentația operațiilor."""

    default_openapi: Callable[[], dict[str, Any]] = app.openapi

    def documented_openapi() -> dict[str, Any]:
        schema = default_openapi()
        paths = schema.get("paths", {})

        schema_operations = {
            (method.upper(), path)
            for path, operations in paths.items()
            for method in operations
        }
        documented_operations = set(OPERATION_DOCS)

        missing = schema_operations - documented_operations
        stale = documented_operations - schema_operations
        if missing or stale:
            details = []
            if missing:
                details.append(f"endpointuri nedocumentate: {_format_operations(missing)}")
            if stale:
                details.append(f"documentație fără endpoint: {_format_operations(stale)}")
            raise RuntimeError("Schema OpenAPI și documentația nu sunt sincronizate; " + "; ".join(details))

        for (method, path), (summary, description) in OPERATION_DOCS.items():
            operation = paths[path][method.lower()]
            operation["summary"] = summary
            operation["description"] = description

        return schema

    app.openapi = documented_openapi


def _format_operations(operations: set[tuple[str, str]]) -> str:
    return ", ".join(f"{method} {path}" for method, path in sorted(operations))
