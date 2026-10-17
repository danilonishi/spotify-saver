class ChannelWatcherManager {
    constructor(apiClient, uiManager, app) {
        this.apiClient = apiClient;
        this.uiManager = uiManager;
        this.app = app;
        this.channels = [];
        this.editingChannelId = null;
        this.pollTimer = null;
        this.refreshInProgress = false;
        this.fastPollingUntil = 0;
        this.pendingManualChecks = new Map();
        this.observedChannelChecks = null;
        this.bindEvents();
    }

    bindEvents() {
        document.getElementById('watcher-interval').addEventListener('change', () => {
            this.updateWeekdayVisibility();
        });
        document.getElementById('watcher-submit-btn').addEventListener('click', () => {
            this.saveChannel();
        });
        document.getElementById('watcher-cancel-btn').addEventListener('click', () => {
            this.cancelEdit();
        });
        document.getElementById('watched-channel-list').addEventListener('click', (event) => {
            const editButton = event.target.closest('[data-edit-channel]');
            if (editButton) {
                event.preventDefault();
                event.stopPropagation();
                this.beginEdit(editButton.dataset.editChannel);
                return;
            }
            const checkButton = event.target.closest('[data-check-channel]');
            if (checkButton) {
                event.preventDefault();
                event.stopPropagation();
                this.checkChannelNow(checkButton);
                return;
            }
            const activeButton = event.target.closest('[data-toggle-channel-active]');
            if (activeButton) {
                event.preventDefault();
                event.stopPropagation();
                this.toggleChannelActive(activeButton);
                return;
            }
            const removeButton = event.target.closest('[data-remove-channel]');
            if (removeButton) {
                event.preventDefault();
                event.stopPropagation();
                this.removeChannel(removeButton.dataset.removeChannel);
            }
        });
    }

    async load() {
        const [channels, status] = await Promise.all([
            this.apiClient.getWatchedChannels(),
            this.apiClient.getChannelWatcherStatus()
        ]);
        this.logChannelCheckChanges(channels);
        this.updateManualCheckStatuses(channels);
        this.channels = channels;
        this.uiManager.renderWatchedChannels(this.channels, status);
        this.uiManager.renderChannelWatcherStatus(status);
    }

    logChannelCheckChanges(channels) {
        const currentChecks = new Map();
        for (const channel of channels) {
            const previous = this.observedChannelChecks?.get(channel.id);
            const current = {
                checkStartedAt: channel.check_started_at,
                lastCheckedAt: channel.last_checked_at,
                lastError: channel.last_error
            };
            currentChecks.set(channel.id, current);

            if (!previous) continue;
            const label = channel.title || channel.url;
            if (!previous.checkStartedAt && current.checkStartedAt) {
                this.uiManager.addLogEntry(`Channel check started: ${label}`, 'info');
            }

            const finished =
                previous.checkStartedAt && !current.checkStartedAt;
            const resultChanged =
                previous.lastCheckedAt !== current.lastCheckedAt ||
                previous.lastError !== current.lastError;
            if (!current.checkStartedAt && (finished || resultChanged)) {
                if (current.lastError) {
                    this.uiManager.addLogEntry(
                        `Channel check failed: ${label} (${current.lastError})`,
                        'error'
                    );
                } else {
                    this.uiManager.addLogEntry(
                        `Channel check completed: ${label}`,
                        'success'
                    );
                }
            }
        }
        this.observedChannelChecks = currentChecks;
    }

    updateManualCheckStatuses(channels) {
        for (const [channelId, check] of this.pendingManualChecks) {
            const channel = channels.find((item) => item.id === channelId);
            if (!channel) {
                this.pendingManualChecks.delete(channelId);
                continue;
            }
            if (channel.check_started_at) {
                check.started = true;
                continue;
            }

            const checkFinished =
                check.started ||
                channel.last_checked_at !== check.lastCheckedAt ||
                channel.last_error !== check.lastError;
            if (!checkFinished) continue;

            this.pendingManualChecks.delete(channelId);
            if (channel.last_error) {
                this.uiManager.updateStatus(
                    `Channel check failed: ${channel.last_error}`,
                    'error'
                );
            } else {
                this.uiManager.updateStatus('Channel check completed', 'success');
            }
        }
    }

    startPolling() {
        if (this.pollTimer) return;
        this.scheduleRefresh(60000);
    }

    scheduleRefresh(delay) {
        if (this.pollTimer) clearTimeout(this.pollTimer);
        this.pollTimer = setTimeout(async () => {
            this.pollTimer = null;
            if (this.refreshInProgress) {
                this.scheduleRefresh(2000);
                return;
            }

            this.refreshInProgress = true;
            try {
                await this.load();
            } catch (error) {
                console.warn('Could not refresh watched channels:', error);
            } finally {
                this.refreshInProgress = false;
                const hasActiveCheck = this.channels.some(
                    (channel) => channel.check_started_at
                );
                const fastPolling = Date.now() < this.fastPollingUntil;
                this.scheduleRefresh(hasActiveCheck || fastPolling ? 2000 : 60000);
            }
        }, delay);
    }

    async saveChannel() {
        const url = document.getElementById('spotify-url').value.trim();
        const outputDir = document.getElementById('watcher-output-dir').value;
        const checkTime = document.getElementById('watcher-time').value;
        if (!url) {
            this.uiManager.updateStatus('Enter a YouTube channel URL', 'error');
            return;
        }
        if (!outputDir) {
            this.uiManager.updateStatus('Choose a download folder for this channel', 'error');
            return;
        }
        const interval = document.getElementById('watcher-interval').value;
        if (interval !== 'manual' && !checkTime) {
            this.uiManager.updateStatus('Choose a check time', 'error');
            return;
        }

        const [checkHour, checkMinute] = checkTime
            ? checkTime.split(':').map(Number)
            : [0, 0];
        const parseKeywords = (id) => document.getElementById(id).value
            .split(',')
            .map((keyword) => keyword.trim())
            .filter(Boolean);
        const durationMinutes = (id) => {
            const value = document.getElementById(id).value;
            return value === '' ? null : Number(value) * 60;
        };
        const settings = {
            platform: 'youtube',
            url,
            output_dir: outputDir,
            interval,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
            check_hour: checkHour,
            check_minute: checkMinute,
            check_weekday: document.getElementById('watcher-interval').value === 'weekly'
                ? Number(document.getElementById('watcher-weekday').value)
                : null,
            video_format: document.getElementById('watcher-video-format').value,
            video_resolution: Number(document.getElementById('watcher-video-resolution').value),
            include_subtitles: document.getElementById('watcher-include-subtitles').checked,
            include_keywords: parseKeywords('watcher-include-keywords'),
            exclude_keywords: parseKeywords('watcher-exclude-keywords'),
            min_duration_seconds: durationMinutes('watcher-min-duration'),
            max_duration_seconds: durationMinutes('watcher-max-duration'),
            published_within_days: document.getElementById('watcher-published-within').value
                ? Number(document.getElementById('watcher-published-within').value)
                : null,
            published_after: document.getElementById('watcher-published-after').value || null,
            published_before: document.getElementById('watcher-published-before').value || null,
            exclude_shorts: document.getElementById('watcher-exclude-shorts').checked,
            exclude_livestreams: document.getElementById('watcher-exclude-livestreams').checked
        };

        const submit = document.getElementById('watcher-submit-btn');
        submit.disabled = true;
        try {
            if (this.editingChannelId) {
                await this.apiClient.updateWatchedChannel(this.editingChannelId, settings);
                this.uiManager.updateStatus('Watched channel settings saved', 'success');
            } else {
                await this.apiClient.createWatchedChannel(settings);
                this.uiManager.updateStatus('Channel added to the watch list', 'success');
            }
            await this.load();
            this.cancelEdit();
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        } finally {
            submit.disabled = false;
        }
    }

    async beginEdit(channelId) {
        const channel = this.channels.find((item) => item.id === channelId);
        if (!channel) return;
        this.editingChannelId = channel.id;
        document.getElementById('spotify-url').value = channel.url;
        document.getElementById('watcher-interval').value = channel.interval;
        document.getElementById('watcher-weekday').value = String(channel.check_weekday ?? 0);
        document.getElementById('watcher-time').value =
            `${String(channel.check_hour).padStart(2, '0')}:${String(channel.check_minute).padStart(2, '0')}`;
        document.getElementById('watcher-video-format').value = channel.video_format;
        document.getElementById('watcher-video-resolution').value = String(channel.video_resolution);
        document.getElementById('watcher-include-subtitles').checked = channel.include_subtitles;
        document.getElementById('watcher-include-keywords').value =
            (channel.include_keywords || []).join(', ');
        document.getElementById('watcher-exclude-keywords').value =
            (channel.exclude_keywords || []).join(', ');
        document.getElementById('watcher-min-duration').value =
            channel.min_duration_seconds == null ? '' : String(channel.min_duration_seconds / 60);
        document.getElementById('watcher-max-duration').value =
            channel.max_duration_seconds == null ? '' : String(channel.max_duration_seconds / 60);
        document.getElementById('watcher-published-within').value =
            channel.published_within_days == null ? '' : String(channel.published_within_days);
        document.getElementById('watcher-published-after').value = channel.published_after || '';
        document.getElementById('watcher-published-before').value = channel.published_before || '';
        document.getElementById('watcher-exclude-shorts').checked = channel.exclude_shorts;
        document.getElementById('watcher-exclude-livestreams').checked =
            channel.exclude_livestreams;
        this.setSubmitButton('Save', 'Save changes to this watched channel');
        document.getElementById('watcher-cancel-btn').classList.remove('hidden');
        this.updateWeekdayVisibility();
        this.app.switchTab('watcher');
        try {
            await this.app.selectWatcherOutputDirectory(channel.output_dir);
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
    }

    async removeChannel(channelId) {
        const channel = this.channels.find((item) => item.id === channelId);
        if (!channel || !confirm(`Stop watching "${channel.title || channel.url}"?`)) return;
        try {
            await this.apiClient.deleteWatchedChannel(channelId);
            if (this.editingChannelId === channelId) this.cancelEdit();
            await this.load();
            this.uiManager.updateStatus('Channel removed from the watch list', 'success');
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        }
    }

    async toggleChannelActive(button) {
        const channelId = button.dataset.toggleChannelActive;
        const channel = this.channels.find((item) => item.id === channelId);
        if (!channel) return;
        button.disabled = true;
        try {
            await this.apiClient.setWatchedChannelActive(channelId, !channel.active);
            await this.load();
            this.uiManager.updateStatus(
                channel.active ? 'Automatic channel checks paused' : 'Automatic channel checks resumed',
                'success'
            );
        } catch (error) {
            this.uiManager.updateStatus(error.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    async checkChannelNow(button) {
        const channelId = button.dataset.checkChannel;
        const channel = this.channels.find((item) => item.id === channelId);
        if (!channel) return;
        this.pendingManualChecks.set(channelId, {
            lastCheckedAt: channel.last_checked_at,
            lastError: channel.last_error,
            started: false
        });
        button.disabled = true;
        let checkAccepted = false;
        try {
            await this.apiClient.checkWatchedChannelNow(channelId);
            checkAccepted = true;
            this.uiManager.updateStatus('Channel check started', 'success');
            this.fastPollingUntil = Date.now() + 30000;
            if (this.pollTimer) {
                clearTimeout(this.pollTimer);
                this.pollTimer = null;
            }
            this.scheduleRefresh(1500);
            await this.load();
        } catch (error) {
            if (!checkAccepted) {
                this.pendingManualChecks.delete(channelId);
                this.uiManager.updateStatus(error.message, 'error');
            } else {
                console.warn('Could not refresh watched channels:', error);
            }
        } finally {
            button.disabled = false;
        }
    }

    cancelEdit() {
        this.editingChannelId = null;
        document.getElementById('spotify-url').value = '';
        document.getElementById('watcher-interval').value = 'daily';
        document.getElementById('watcher-weekday').value = '0';
        document.getElementById('watcher-time').value = '09:00';
        document.getElementById('watcher-video-format').value = 'mp4';
        document.getElementById('watcher-video-resolution').value = '0';
        document.getElementById('watcher-include-subtitles').checked = false;
        document.getElementById('watcher-include-keywords').value = '';
        document.getElementById('watcher-exclude-keywords').value = '';
        document.getElementById('watcher-min-duration').value = '';
        document.getElementById('watcher-max-duration').value = '';
        document.getElementById('watcher-published-within').value = '';
        document.getElementById('watcher-published-after').value = '';
        document.getElementById('watcher-published-before').value = '';
        document.getElementById('watcher-exclude-shorts').checked = false;
        document.getElementById('watcher-exclude-livestreams').checked = false;
        this.setSubmitButton('Watch Channel', 'Add this channel to the watch list');
        document.getElementById('watcher-cancel-btn').classList.add('hidden');
        this.updateWeekdayVisibility();
        document.getElementById('watcher-output-dir').value = '';
        document.querySelectorAll('#watcher-output-dir-options [data-output-dir]').forEach((button) => {
            button.classList.remove('selected');
            button.setAttribute('aria-pressed', 'false');
        });
        const defaultFolder = document.querySelector(
            '#watcher-output-dir-options [data-output-dir]'
        );
        if (defaultFolder) {
            this.uiManager.selectOutputDirectory(defaultFolder, 'watcher');
        }
    }

    updateWeekdayVisibility() {
        const interval = document.getElementById('watcher-interval').value;
        document.getElementById('watcher-weekday-group').classList.toggle(
            'hidden', interval !== 'weekly'
        );
        document.getElementById('watcher-time-group').classList.toggle(
            'hidden', interval === 'manual'
        );
    }

    setSubmitButton(label, title) {
        const button = document.getElementById('watcher-submit-btn');
        document.getElementById('watcher-submit-label').textContent = label;
        button.title = title;
    }
}
