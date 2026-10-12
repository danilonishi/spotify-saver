class ApiClient {
    constructor() {
        this.apiUrl = `${window.location.protocol}//${window.location.hostname}:8000/api/v1`;
        this.apiUrlHealth = `${window.location.protocol}//${window.location.hostname}:8000/health`;
        this.apiUrlVersion = `${window.location.protocol}//${window.location.hostname}:8000/version`;
        this.maxRetries = 3;
    }

    async checkApiStatusWithRetry() {
        for (let i = 0; i < this.maxRetries; i++) {
            const success = await this.checkApiStatus();
            if (success) {
                return true;
            }
            
            if (i < this.maxRetries - 1) {
                await new Promise(resolve => setTimeout(resolve, 2000 * (i + 1))); // Exponential backoff
            }
        }
        return false;
    }

    async checkApiStatus() {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 5000); // 5 second timeout
            
            const response = await fetch(this.apiUrlHealth, {
                signal: controller.signal,
                method: 'GET',
                mode: 'cors',
                credentials: 'omit',
                cache: 'no-cache'
            });
            
            clearTimeout(timeoutId);
            
            if (response.ok) {
                return true;
            } else {
                return false;
            }
        } catch (error) {
            console.warn('API status check failed:', error);
            return false;
        }
    }

    async getDefaultOutputDir() {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 3000); // 3 second timeout
            
            const response = await fetch(`${this.apiUrl}/config/output_dir`, {
                signal: controller.signal,
                method: 'GET',
                mode: 'cors',
                cache: 'no-cache'
            });
            
            clearTimeout(timeoutId);
            
            if (response.ok) {
                const data = await response.json();
                return data.output_dir || 'Music';
            } else {
                return 'Music';
            }
        } catch (error) {
            console.warn('Could not fetch default output directory, using fallback:', error);
            return 'Music';
        }
    }

    async getOutputDirectories(rootPath = null, mediaType = 'audio') {
        const url = new URL(`${this.apiUrl}/config/output_dirs`);
        if (rootPath) {
            url.searchParams.set('root', rootPath);
        }
        url.searchParams.set('media_type', mediaType);

        const response = await fetch(url, {
            method: 'GET',
            mode: 'cors',
            cache: 'no-cache'
        });

        if (!response.ok) {
            throw new Error('Could not use the specified base media path');
        }

        const data = await response.json();
        return {
            root: data.root,
            directories: data.directories || []
        };
    }

    async createOutputDirectory(rootPath, name) {
        const response = await fetch(`${this.apiUrl}/config/output_dirs`, {
            method: 'POST',
            mode: 'cors',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ root: rootPath, name })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not create the folder');
        }

        return response.json();
    }

    async getAppVersion() {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 3000); // 3 second timeout
            
            const response = await fetch(this.apiUrlVersion, {
                signal: controller.signal,
                method: 'GET',
                mode: 'cors',
                cache: 'no-cache'
            });
            
            clearTimeout(timeoutId);
            
            if (response.ok) {
                const data = await response.json();
                return data.version || null;
            } else {
                console.warn('Could not fetch app version from API');
                return null;
            }
        } catch (error) {
            console.warn('Could not fetch app version, using fallback:', error);
            return null;
        }
    }

    async inspectSpotifyUrl(spotifyUrl) {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 120000); // OAuth may require browser authorization

        try {
            const response = await fetch(`${this.apiUrl}/inspect?spotify_url=${encodeURIComponent(spotifyUrl)}`, {
                signal: controller.signal,
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                }
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
                throw new Error(errorData.detail || 'Error inspecting URL');
            }

            return await response.json();
        } catch (error) {
            if (error.name === 'AbortError') {
                throw new Error('Spotify authorization timed out. Complete the Spotify login and try again.');
            }
            throw error;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    async startDownload(formData) {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 30000); // 30 second timeout

        const response = await fetch(`${this.apiUrl}/download`, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            body: JSON.stringify(formData),
            signal: controller.signal
        });

        clearTimeout(timeoutId);

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Download error');
        }

        return await response.json();
    }

    async getDownloadStatus(taskId) {
        try {
            const response = await fetch(`${this.apiUrl}/download/${taskId}/status`);
            if (response.ok) {
                return await response.json();
            }
            return null;
        } catch (error) {
            console.warn('Error checking download status:', error);
            return null;
        }
    }

    async cancelDownload(taskId) {
        const response = await fetch(`${this.apiUrl}/download/${taskId}/cancel`, {
            method: 'POST',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Could not stop download');
        }

        return await response.json();
    }

    async retryDownload(taskId) {
        const response = await fetch(`${this.apiUrl}/download/${taskId}/retry`, {
            method: 'POST',
            headers: { 'Accept': 'application/json' }
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Could not retry download');
        }
        return await response.json();
    }

    async removeDownload(taskId) {
        const response = await fetch(`${this.apiUrl}/download/${taskId}`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Could not remove download');
        }

        return await response.json();
    }

    async getQueue() {
        const response = await fetch(`${this.apiUrl}/queue`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            throw new Error('Could not fetch the download queue');
        }

        return await response.json();
    }

    async pauseQueue() {
        const response = await fetch(`${this.apiUrl}/queue/pause`, {
            method: 'POST',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Could not pause the queue');
        }

        return await response.json();
    }

    async resumeQueue() {
        const response = await fetch(`${this.apiUrl}/queue/resume`, {
            method: 'POST',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(errorData.detail || 'Could not resume the queue');
        }

        return await response.json();
    }

    async clearQueuedDownloads() {
        const response = await fetch(`${this.apiUrl}/queue`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            throw new Error('Could not clear the queue');
        }

        return await response.json();
    }

    async clearCompletedDownloads() {
        const response = await fetch(`${this.apiUrl}/queue/completed`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
        });

        if (!response.ok) {
            throw new Error('Could not clear completed downloads');
        }

        return await response.json();
    }

    async getWatchedChannels() {
        const response = await fetch(`${this.apiUrl}/channel-watcher`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' }
        });
        if (!response.ok) {
            throw new Error('Could not fetch watched channels');
        }
        return response.json();
    }

    async getDownloadDirectoryPreferences() {
        const response = await fetch(`${this.apiUrl}/preferences/download-directories`, {
            method: 'GET',
            mode: 'cors',
            cache: 'no-cache'
        });
        if (!response.ok) {
            throw new Error('Could not fetch saved download folders');
        }
        return response.json();
    }

    async saveDownloadDirectoryPreference(kind, directory) {
        const response = await fetch(
            `${this.apiUrl}/preferences/download-directories/${encodeURIComponent(kind)}`,
            {
                method: 'PUT',
                mode: 'cors',
                credentials: 'omit',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ directory })
            }
        );
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not save the download folder');
        }
        return response.json();
    }

    async getJellyfinLibraryScanSettings() {
        const response = await fetch(`${this.apiUrl}/preferences/jellyfin-library-scan`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            cache: 'no-cache'
        });
        if (!response.ok) {
            throw new Error('Could not fetch Jellyfin library scan settings');
        }
        return response.json();
    }

    async saveJellyfinLibraryScanSettings(settings) {
        const response = await fetch(`${this.apiUrl}/preferences/jellyfin-library-scan`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not save Jellyfin settings');
        }
        return response.json();
    }

    async removeJellyfinApiKey() {
        const response = await fetch(
            `${this.apiUrl}/preferences/jellyfin-library-scan/api-key`,
            { method: 'DELETE' }
        );
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not remove the Jellyfin API key');
        }
        return response.json();
    }

    async requestJellyfinLibraryScan() {
        const response = await fetch(`${this.apiUrl}/jellyfin-library-scan`, {
            method: 'POST'
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not request a Jellyfin library scan');
        }
        return response.json();
    }

    async getChannelWatcherStatus() {
        const response = await fetch(`${this.apiUrl}/channel-watcher/status`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' }
        });
        if (!response.ok) {
            throw new Error('Could not fetch channel watcher status');
        }
        return response.json();
    }

    async createWatchedChannel(settings) {
        return this.sendWatchedChannelRequest('/channel-watcher', 'POST', settings);
    }

    async updateWatchedChannel(channelId, settings) {
        return this.sendWatchedChannelRequest(
            `/channel-watcher/${encodeURIComponent(channelId)}`,
            'PUT',
            settings
        );
    }

    async setWatchedChannelActive(channelId, active) {
        return this.sendWatchedChannelRequest(
            `/channel-watcher/${encodeURIComponent(channelId)}/active`,
            'PUT',
            { active }
        );
    }

    async checkWatchedChannelNow(channelId) {
        const response = await fetch(
            `${this.apiUrl}/channel-watcher/${encodeURIComponent(channelId)}/check`,
            {
                method: 'POST',
                headers: { 'Accept': 'application/json' }
            }
        );
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not start channel check');
        }
        return response.json();
    }

    async sendWatchedChannelRequest(path, method, settings) {
        const response = await fetch(`${this.apiUrl}${path}`, {
            method,
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            body: JSON.stringify(settings)
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not save watched channel');
        }
        return response.json();
    }

    async deleteWatchedChannel(channelId) {
        const response = await fetch(
            `${this.apiUrl}/channel-watcher/${encodeURIComponent(channelId)}`,
            {
                method: 'DELETE',
                headers: { 'Accept': 'application/json' }
            }
        );
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Could not remove watched channel');
        }
        return response.json();
    }
}