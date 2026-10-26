# 🤖 Gemini Web Interpreter

[![License: MIT](https://shields.io)](https://opensource.org)
[![Python 3.10+](https://shields.io)](https://python.org)
[![Playwright](https://shields.io)](https://playwright.dev)

**Gemini Web Interpreter** transforme Google Gemini en un véritable agent autonome capable de contrôler votre machine Linux (Ubuntu) directement depuis une interface Web locale et moderne. 

Grâce à une boucle d'exécution automatisée, Gemini analyse vos demandes, écrit le code (Python ou Bash), l'exécute localement et corrige ses propres erreurs en temps réel jusqu'à accomplir la tâche demandée.

---

## ✨ Fonctionnalités

- 🌐 **Interface Web Intuitive :** Un chat fluide (Flask) pour interagir avec Gemini, voir le code généré et suivre les sorties du terminal.
- ⚙️ **Mode Automatique :** Laissez l'IA enchaîner les étapes sans intervention humaine (jusqu'à 15 étapes consécutives).
- 🛡️ **Sécurité Intégrée :** Détection automatique des commandes dangereuses (`rm -rf`, `sudo`, `chmod 777`, etc.) nécessitant une confirmation manuelle explicite.
- 📁 **Gestion des Fichiers :** Importez des fichiers directement dans l'interface pour que Gemini les analyse ou les traite.
- 🔄 **Profil Persistant :** Utilise un profil Chrome dédié pour conserver votre session Google sans interférer avec votre navigateur personnel.

---

## 🚀 Installation & Lancement

### Prérequis
Assurez-vous d'avoir Python 3.10+ d'installé sur votre système Linux (Ubuntu recommandé).

### 1. Cloner le projet
```bash
git clone https://github.com
cd gemini-web-interpreter
```

### 2. Installer les dépendances
```bash
pip install flask playwright
playwright install chromium
```

### 3. Lancer l'application
```bash
python gemini_web.py
```
Ouvrez ensuite votre navigateur sur [http://127.0.0.1:5000](http://127.0.0.1:5000).

> 💡 **Note :** Lors du premier lancement, connectez-vous à votre compte Google sur l'interface Gemini si nécessaire. La session sera définitivement sauvegardée dans votre profil local isolé.

---

## 🛡️ Sécurité & Transparence

Ce script s'exécute avec les privilèges de votre session utilisateur locale. Bien qu'un filtre de mots-clés dangereux soit actif (`DANGEROUS_PATTERNS`), l'exécution de code généré par IA comporte des risques. **Utilisez le mode automatique avec précaution.**

---



Les contributions, signalements de bugs et suggestions de fonctionnalités sont les bienvenus ! N'hésitez pas à ouvrir une *Issue* ou à soumettre une *Pull Request*.

⭐️ **Si ce projet vous plaît, n'hésitez pas à lui laisser une étoile pour lui donner de la visibilité !**
