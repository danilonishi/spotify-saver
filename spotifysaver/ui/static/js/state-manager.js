class StateManager {
    constructor() {
        this.storageKey = 'spotifysaver_state';
    }

    saveState() {
        const state = {
            lastUrl: document.getElementById('spotify-url').value,
            logs: this.getLogs(),
            timestamp: Date.now()
        };
        localStorage.setItem(this.storageKey, JSON.stringify(state));
    }

    loadPersistedState() {
        try {
            const saved = localStorage.getItem(this.storageKey);
            if (!saved) return null;
            
            const state = JSON.parse(saved);
            const maxAge = 24 * 60 * 60 * 1000; // 24 horas
            
            // Check whether the state is recent enough
            if (Date.now() - state.timestamp > maxAge) {
                localStorage.removeItem(this.storageKey);
                return null;
            }

            return state;
        } catch (error) {
            console.warn('Error loading persisted state:', error);
            localStorage.removeItem(this.storageKey);
            return null;
        }
    }

    clearPersistedState() {
        localStorage.removeItem(this.storageKey);
    }

    getLogs() {
        const logEntries = document.querySelectorAll('.log-entry');
        // Reverse to preserve the original chronological order when saving
        return Array.from(logEntries).reverse().map(entry => ({
            text: entry.textContent,
            className: entry.className
        }));
    }

    restoreLogs(logs) {
        const logContent = document.getElementById('log-content');
        logContent.innerHTML = '';
        // Reverse the log order to show the most recent entries first
        logs.reverse().forEach(log => {
            const entry = document.createElement('div');
            entry.className = log.className;
            entry.textContent = log.text;
            logContent.appendChild(entry);
        });
        logContent.scrollTop = 0;
    }

    restoreFormData(state) {
        // Restaurar URL
        if (state.lastUrl) {
            document.getElementById('spotify-url').value = state.lastUrl;
        }

        // Restaurar logs
        if (state.logs && state.logs.length > 0) {
            this.restoreLogs(state.logs);
        }

    }
}