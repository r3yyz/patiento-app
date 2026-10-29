// Outils communs : flux temps réel, appels API, notifications.

function ecouterEtat(surEtat, indicateur) {
  let source;
  let minuteur;
  const marquer = (ok) => {
    if (!indicateur) return;
    indicateur.classList.toggle("ok", ok);
    indicateur.textContent = ok ? "Connecté" : "Reconnexion…";
  };
  const connecter = () => {
    source = new EventSource("/evenements");
    source.onmessage = (e) => { marquer(true); surEtat(JSON.parse(e.data)); };
    source.onerror = () => {
      marquer(false);
      if (source.readyState === EventSource.CLOSED) {
        clearTimeout(minuteur);
        minuteur = setTimeout(connecter, 3000);
      }
    };
  };
  connecter();
}

async function envoyerJson(url, donnees) {
  const rep = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(donnees || {}),
  });
  const corps = await rep.json().catch(() => ({}));
  if (!rep.ok) throw new Error(corps.erreur || "Erreur du serveur.");
  return corps;
}

function notifier(texte, erreur = false) {
  let n = document.querySelector(".notification");
  if (!n) {
    n = document.createElement("div");
    n.className = "notification";
    n.setAttribute("role", "status");
    document.body.appendChild(n);
  }
  n.textContent = texte;
  n.classList.toggle("erreur", erreur);
  n.classList.add("visible");
  clearTimeout(n._minuteur);
  n._minuteur = setTimeout(() => n.classList.remove("visible"), 3200);
}

function elt(balise, attributs = {}, ...enfants) {
  const e = document.createElement(balise);
  for (const [cle, valeur] of Object.entries(attributs)) {
    if (cle === "onclick") e.addEventListener("click", valeur);
    else if (cle === "class") e.className = valeur;
    else e.setAttribute(cle, valeur);
  }
  for (const enfant of enfants) {
    if (enfant !== null && enfant !== undefined) e.append(enfant);
  }
  return e;
}

function ilYa(secondes) {
  const s = Math.max(0, Math.round(secondes));
  if (s < 60) return "à l'instant";
  return `il y a ${Math.round(s / 60)} min`;
}
