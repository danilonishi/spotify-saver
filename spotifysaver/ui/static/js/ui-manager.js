class UIManager {
    constructor(saveStateCallback = null) {
        this.recentLogs = new Set();
        this.logCooldownTime = 2000; // 2 seconds
        this.saveStateCallback = saveStateCallback;
        // Set by the app once the DownloadManager exists; invoked with a queue item's task id.
        this.onRemoveQueueItem = null;
        this.onRetryQueueItem = null;
        this.onOutputDirectorySelected = null;
        this.activeMode = 'audio';
        this.downloading = false;
        this.preferredOutputDirectories = {
            audio: null,
            video: null,
            watcher: null
        };
    }

    setActiveMode(mode) {
        this.activeMode = mode;
        this.updateDownloadButtonLabel();
    }

    updateDownloadButtonLabel() {
        const button = document.getElementById('download-btn');
        if (this.downloading) {
            button.textContent = 'Enqueue Download';
            return;
        }

        const icon = document.createElement('span');
        icon.className = this.activeMode === 'video'
            ? 'music-note video-download-icon'
            : 'music-note audio-download-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = this.activeMode === 'video' ? '🎬︎' : '♫︎';
        button.replaceChildren(icon, document.createTextNode(' Start Download'));
    }

    updateUI(downloading, paused = false) {
        const downloadBtn = document.getElementById('download-btn');
        const stopDownloadBtn = document.getElementById('stop-download-btn');
        const progressContainer = document.getElementById('progress-container');

        downloadBtn.disabled = false;
        this.downloading = downloading;
        this.updateDownloadButtonLabel();
        if (downloading) {
            stopDownloadBtn.disabled = paused;
            stopDownloadBtn.textContent = paused ? 'Stopping...' : 'Stop download';
            progressContainer.classList.remove('hidden');
        } else if (paused) {
            stopDownloadBtn.disabled = false;
            stopDownloadBtn.textContent = 'Resume download';
            progressContainer.classList.add('hidden');
        } else {
            stopDownloadBtn.disabled = true;
            stopDownloadBtn.textContent = 'Stop download';
            progressContainer.classList.add('hidden');
            this.updateProgress(0);
        }
    }

    renderDownloadQueue(queue) {
        const list = document.getElementById('download-queue');
        const clearButton = document.getElementById('clear-queue-btn');
        const clearCompletedButton = document.getElementById('clear-completed-btn');
        const expandedItems = new Set(
            Array.from(list.querySelectorAll('.queue-item-details[open]'), (item) => item.dataset.queueId)
        );
        list.replaceChildren();

        if (!queue.length) {
            const emptyMessage = document.createElement('li');
            emptyMessage.className = 'queue-empty';
            emptyMessage.textContent = 'Queue is empty';
            list.appendChild(emptyMessage);
        }

        queue.forEach((item) => {
            const entry = document.createElement('li');
            entry.className = `queue-item queue-item-${item.status}`;

            const details = document.createElement('details');
            details.className = 'queue-item-details';
            details.dataset.queueId = String(item.id);
            details.open = expandedItems.has(String(item.id));

            const summary = document.createElement('summary');
            summary.className = 'queue-item-summary';
            summary.title = 'Show or hide download details';

            const name = document.createElement('span');
            name.className = 'queue-item-name';
            name.textContent = item.title;
            name.title = item.title;

            const status = document.createElement('span');
            status.className = 'queue-item-status';
            const statusLabels = {
                queued: 'Queued',
                downloading: 'Downloading',
                paused: 'Paused',
                completed: 'Completed',
                failed: 'Failed',
                cancelled: 'Cancelled'
            };
            status.textContent = item.status === 'downloading' && item.progress != null
                ? `Downloading ${Math.round(item.progress)}%`
                : item.status === 'completed_with_errors'
                    ? 'Completed with errors'
                    : statusLabels[item.status] || item.status;
            status.title = `Download status: ${status.textContent}`;

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'queue-item-remove-btn';
            removeBtn.title = 'Remove enqueued item';
            removeBtn.setAttribute('aria-label', 'Remove enqueued item');
            removeBtn.textContent = '❌';
            removeBtn.addEventListener('click', (event) => {
                // Prevent the click from toggling the parent <details> open/closed.
                event.preventDefault();
                event.stopPropagation();
                this.onRemoveQueueItem?.(item.id, item.status);
            });

            if (['failed', 'completed_with_errors'].includes(item.status)) {
                const retryBtn = document.createElement('button');
                retryBtn.type = 'button';
                retryBtn.className = 'queue-item-retry-btn';
                retryBtn.title = 'Retry failed download';
                retryBtn.setAttribute('aria-label', 'Retry failed download');
                retryBtn.textContent = '↻';
                retryBtn.addEventListener('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    this.onRetryQueueItem?.(item.id);
                });
                summary.append(name, status, retryBtn, removeBtn);
            } else {
                summary.append(name, status, removeBtn);
            }
            details.appendChild(summary);

            if (item.status === 'failed' && item.error_message) {
                const errorMessage = document.createElement('p');
                errorMessage.className = 'status-error queue-item-error';
                errorMessage.textContent = item.error_message;
                details.appendChild(errorMessage);
            }

            const tracks = Array.isArray(item.trackData?.tracks)
                ? item.trackData.tracks
                : item.trackData?.name && item.trackData?.artists
                    ? [item.trackData]
                    : [];
            if (tracks.length) {
                const trackList = document.createElement('ul');
                trackList.className = 'queue-track-list';

                tracks.forEach((track, index) => {
                    const trackNumber = track.number || index + 1;
                    const trackState = item.trackStates?.get(trackNumber) || 'waiting';
                    const trackEntry = document.createElement('li');
                    trackEntry.className = `track-state-${trackState}`;
                    trackEntry.dataset.trackNumber = String(trackNumber);
                    trackEntry.dataset.trackState = trackState;

                    const icon = document.createElement('span');
                    icon.className = 'track-icon';
                    icon.textContent = this.getStateIcon(trackState);

                    const duration = track.duration || 0;
                    const minutes = Math.floor(duration / 60);
                    const seconds = String(duration % 60).padStart(2, '0');
                    const info = document.createElement('span');
                    info.className = 'queue-track-info';
                    const artists = (track.artists || []).join(', ');
                    const artistsText = artists ? ` — ${artists}` : '';
                    const durationText = duration ? ` [${minutes}:${seconds}]` : '';
                    info.textContent = `${trackNumber}. ${track.name}${artistsText}${durationText}`;
                    info.title = info.textContent;

                    trackEntry.append(icon, info);
                    trackList.appendChild(trackEntry);
                });
                details.appendChild(trackList);
            } else if (item.formData?.media_type === 'video') {
                const videoDetails = document.createElement('p');
                videoDetails.className = 'queue-track-empty';
                videoDetails.textContent = `Video source: ${item.url}`;
                details.appendChild(videoDetails);
            } else if (this.isYouTubeTrackUrl(item.url)) {
                const youtubeDetails = document.createElement('p');
                youtubeDetails.className = 'queue-track-empty';
                youtubeDetails.textContent = `YouTube audio source: ${item.url}`;
                details.appendChild(youtubeDetails);
            } else {
                const trackMessage = document.createElement('p');
                trackMessage.className = 'queue-track-empty';
                if (item.status === 'failed') {
                    trackMessage.textContent = '';
                } else if (item.isYouTubeCollection) {
                    trackMessage.textContent = item.playlistVerification
                        ? `${item.playlistVerification === 'rechecking' ? 'Re-verifying' : 'Checking'} YouTube playlist entries${item.resolvedTitle ? ` for ${item.resolvedTitle}` : ''}...`
                        : 'Playlist entries will be checked when this download starts.';
                    trackMessage.setAttribute('aria-live', 'polite');
                } else {
                    trackMessage.textContent = 'Track details appear when this download starts.';
                }
                if (trackMessage.textContent) {
                    details.appendChild(trackMessage);
                }
            }

            if (item.output_directory) {
                const outputDirectory = document.createElement('p');
                outputDirectory.className = 'queue-track-empty';
                outputDirectory.textContent = `Saved to: ${item.output_directory}`;
                details.appendChild(outputDirectory);
            }

            entry.appendChild(details);
            list.appendChild(entry);
        });

        clearButton.disabled = !queue.some((item) =>
            item.status !== 'downloading' ||
            (
                ['playlist', 'album'].includes(item.formData?.content_type) &&
                item.trackData?.tracks?.length > 1
            )
        );
        clearCompletedButton.disabled = !queue.some((item) =>
            ['completed', 'completed_with_errors', 'failed', 'cancelled'].includes(item.status)
        );
    }

    renderWatchedChannels(channels, status = {}) {
        const cooldownUntil = status.youtube_cooldown_until
            ? new Date(status.youtube_cooldown_until)
            : null;
        const watcherCoolingDown = cooldownUntil && cooldownUntil > new Date();
        const list = document.getElementById('watched-channel-list');
        const expandedItems = new Set(
            Array.from(list.querySelectorAll('.queue-item-details[open]'), (item) => item.dataset.channelId)
        );
        list.replaceChildren();

        if (!channels.length) {
            const emptyMessage = document.createElement('li');
            emptyMessage.className = 'queue-empty';
            emptyMessage.textContent = 'No watched channels';
            list.appendChild(emptyMessage);
            return;
        }

        channels.forEach((channel) => {
            const entry = document.createElement('li');
            entry.className = 'queue-item';
            if (!channel.active) entry.classList.add('watched-channel-paused');

            const details = document.createElement('details');
            details.className = 'queue-item-details watched-channel-details';
            details.dataset.channelId = channel.id;
            details.open = expandedItems.has(channel.id);

            const summary = document.createElement('summary');
            summary.className = 'queue-item-summary';
            summary.title = 'Show or hide channel details';

            const name = document.createElement('span');
            name.className = 'queue-item-name';
            name.textContent = channel.title || channel.url;
            name.title = channel.url;

            const interval = document.createElement('span');
            interval.className = 'queue-item-status watched-channel-summary-status';
            const intervalLabel = {
                manual: 'Manual',
                hourly: 'Hourly',
                daily: 'Daily',
                weekly: 'Weekly'
            }[channel.interval] || channel.interval;
            const lastCheckedDate = channel.last_checked_at
                ? new Date(channel.last_checked_at)
                : null;
            const checkIsOverdue = channel.active
                && channel.interval !== 'manual'
                && !watcherCoolingDown
                && new Date(channel.next_check_at).getTime() <= Date.now()
                && !channel.check_started_at;
            const lastCheckedLabel = lastCheckedDate
                ? `Last checked ${lastCheckedDate.toLocaleString()}`
                : 'Not checked yet';
            const summaryLastChecked = lastCheckedDate
                ? `Last checked ${lastCheckedDate.toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit'
                })}`
                : lastCheckedLabel;
            interval.textContent = channel.interval === 'manual'
                ? `${intervalLabel} · ${summaryLastChecked}${channel.active ? '' : ' · Paused'}`
                : checkIsOverdue
                    ? `${intervalLabel} · Check overdue`
                    : `${intervalLabel} · ${summaryLastChecked}${channel.active ? '' : ' · Paused'}`;
            interval.title = channel.interval === 'manual'
                ? `${lastCheckedLabel}; checks run only when started manually${channel.active ? '' : '; automatic checks are paused'}`
                : checkIsOverdue
                    ? `${lastCheckedLabel}; scheduled check is overdue and no check is reported in progress`
                    : `${lastCheckedLabel}${channel.active ? '' : '; automatic checks are paused'}`;

            const removeButton = document.createElement('button');
            removeButton.type = 'button';
            removeButton.className = 'queue-item-remove-btn';
            removeButton.title = 'Remove watched channel';
            removeButton.setAttribute('aria-label', 'Remove watched channel');
            removeButton.dataset.removeChannel = channel.id;
            removeButton.textContent = '❌';

            summary.append(name, interval, removeButton);
            details.appendChild(summary);

            const lastChecked = document.createElement('p');
            lastChecked.className = 'queue-track-empty watched-channel-check';
            lastChecked.textContent = channel.last_checked_at
                ? `Last checked: ${new Date(channel.last_checked_at).toLocaleString()}`
                : 'Last checked: Not checked yet';
            details.appendChild(lastChecked);

            if (channel.interval !== 'manual' && channel.active) {
                const nextCheck = document.createElement('p');
                nextCheck.className = 'queue-track-empty watched-channel-check';
                nextCheck.textContent = `Next check: ${new Date(channel.next_check_at).toLocaleString()}`;
                details.appendChild(nextCheck);
            } else if (!channel.active && channel.interval !== 'manual') {
                const paused = document.createElement('p');
                paused.className = 'queue-track-empty watched-channel-check';
                paused.textContent = 'Automatic checks are paused.';
                details.appendChild(paused);
            }

            if (checkIsOverdue) {
                const overdue = document.createElement('p');
                overdue.className = 'status-error watched-channel-error';
                overdue.textContent =
                    'Scheduled check is overdue, but the server has not reported a check in progress.';
                details.appendChild(overdue);
            }

            if (channel.check_started_at) {
                const checking = document.createElement('p');
                checking.className = 'queue-track-empty watched-channel-check';
                checking.textContent =
                    `Check in progress since: ${new Date(channel.check_started_at).toLocaleString()}`;
                details.appendChild(checking);
            }

            if (channel.last_error) {
                const error = document.createElement('p');
                error.className = 'status-error watched-channel-error';
                error.textContent = `Last check failed: ${channel.last_error}`;
                details.appendChild(error);
            }

            const actions = document.createElement('div');
            actions.className = 'watched-channel-actions';

            const checkButton = document.createElement('button');
            checkButton.type = 'button';
            checkButton.className = 'output-dir-button watched-channel-check-now';
            checkButton.dataset.checkChannel = channel.id;
            checkButton.textContent = channel.check_started_at ? 'Checking…' : 'Check now';
            checkButton.title = channel.check_started_at
                ? 'A channel check is already running'
                : watcherCoolingDown
                    ? 'Channel checks are temporarily paused by YouTube'
                    : 'Check this channel for new uploads now';
            checkButton.disabled = Boolean(
                channel.check_started_at || watcherCoolingDown
            );

            const activeButton = document.createElement('button');
            activeButton.type = 'button';
            activeButton.className = 'output-dir-button watched-channel-active-toggle';
            activeButton.dataset.toggleChannelActive = channel.id;
            activeButton.textContent = channel.active ? 'Pause' : 'Resume';
            activeButton.title = channel.active
                ? 'Pause automatic channel checks'
                : 'Resume automatic channel checks';
            activeButton.setAttribute('aria-label', activeButton.title);
            activeButton.setAttribute('aria-pressed', String(channel.active));

            const editButton = document.createElement('button');
            editButton.type = 'button';
            editButton.className = 'output-dir-button watched-channel-edit';
            editButton.dataset.editChannel = channel.id;
            editButton.textContent = 'Edit settings';
            editButton.title = 'Edit this channel’s settings';
            actions.append(checkButton, activeButton, editButton);
            details.appendChild(actions);

            entry.appendChild(details);
            list.appendChild(entry);
        });
    }

    renderChannelWatcherStatus(status) {
        const message = document.getElementById('channel-watcher-status');
        const cooldownUntil = status.youtube_cooldown_until
            ? new Date(status.youtube_cooldown_until)
            : null;
        if (!cooldownUntil || cooldownUntil <= new Date()) {
            message.textContent = '';
            message.classList.add('hidden');
            return;
        }

        message.textContent =
            `YouTube rate-limit cooldown active. Channel checks are paused until ${cooldownUntil.toLocaleString()}.`;
        message.classList.remove('hidden');
    }

    updateQueuedTrackState(queueItemId, trackNumber, state) {
        const details = document.querySelector(`.queue-item-details[data-queue-id="${queueItemId}"]`);
        const track = details?.querySelector(`[data-track-number="${trackNumber}"]`);
        if (!track) return;

        track.className = track.className.replace(/track-state-\w+/g, '');
        track.classList.add(`track-state-${state}`);
        track.dataset.trackState = state;
        track.querySelector('.track-icon').textContent = this.getStateIcon(state);
    }

    updateStatus(message, type = 'info') {
        const statusMessage = document.getElementById('status-message');
        statusMessage.textContent = message;
        statusMessage.className = `status-${type}`;
    }

    updateProgress(percentage) {
        const progressFill = document.getElementById('progress-fill');
        const progressText = document.getElementById('progress-text');
        
        progressFill.style.width = `${percentage}%`;
        progressText.textContent = `${Math.round(percentage)}%`;
    }

    addLogEntry(message, type = 'info') {
        // Prevent duplicate logs by using a unique identifier
        const logId = `${type}:${message}`;
        
        // Check whether this message was logged recently
        if (this.recentLogs.has(logId)) {
            return; // Do not add duplicate logs
        }
        
        // Add to the recent log cache with a cooldown
        this.recentLogs.add(logId);
        setTimeout(() => {
            this.recentLogs.delete(logId);
        }, this.logCooldownTime);
        
        const logContent = document.getElementById('log-content');
        const timestamp = new Date().toLocaleTimeString();
        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        entry.textContent = `[${timestamp}] ${message}`;
        
        // Insert at the top to show the most recent entries first
        logContent.insertBefore(entry, logContent.firstChild);
        logContent.scrollTop = 0;
        
        // Save state after adding a log
        if (this.saveStateCallback) {
            this.saveStateCallback();
        }
    }

    clearLog() {
        const logContent = document.getElementById('log-content');
        logContent.innerHTML = '';
        
        // Also clear the deduplication state
        this.recentLogs.clear();
        
        // Force-clear the state cache
        console.log('🧹 Clearing all track states and cache');
    }

    isYouTubeTrackUrl(url) {
        try {
            const parsedUrl = new URL(url);
            const hosts = new Set([
                'music.youtube.com',
                'youtube.com',
                'www.youtube.com',
                'm.youtube.com',
                'youtu.be'
            ]);
            if (!hosts.has(parsedUrl.hostname.toLowerCase())) {
                return false;
            }
            return parsedUrl.hostname.toLowerCase() === 'youtu.be' ||
                parsedUrl.pathname.replace(/\/$/, '') === '/watch';
        } catch {
            return false;
        }
    }

    getStateIcon(state) {
        const icons = {
            'waiting': '⏳',      // Hourglass - Waiting
            'downloading': '🔄', // Blue arrows - Downloading
            'completed': '✅',   // Green check - Completed
            'error': '❌'         // Red X - Error
        };
        const icon = icons[state] || '⏳';
        console.log(`📍 getStateIcon(${state}) -> ${icon}`);
        return icon;
    }

    setOutputDirectories(directories, rootPath, kind = 'audio') {
        const picker = UIManager.PICKERS[kind];
        const options = document.getElementById(picker.options);
        const outputDirInput = document.getElementById(picker.hidden);
        const mediaRootInput = document.getElementById(picker.rootInput);
        options.replaceChildren();
        if (rootPath) {
            mediaRootInput.value = rootPath;
            this.updatePathDisplay(rootPath, '', kind);

            const parentPath = this.getParentPath(rootPath);
            if (parentPath) {
                const parentButton = document.createElement('button');
                parentButton.type = 'button';
                parentButton.className = 'output-dir-button parent-dir-button';
                parentButton.dataset.parentDir = parentPath;
                parentButton.textContent = '[Parent]';
                parentButton.title = 'Open the parent folder';
                options.appendChild(parentButton);
            }
        }

        const savedPath = this.preferredOutputDirectories[kind];
        const savedIsRoot = Boolean(rootPath && savedPath === rootPath);
        const rootButton = savedIsRoot ? document.createElement('button') : null;
        if (rootButton) {
            rootButton.type = 'button';
            rootButton.className = 'output-dir-button root-dir-button';
            rootButton.dataset.outputDir = rootPath;
            rootButton.textContent = '[Use this folder]';
            rootButton.title = `Select the base folder: ${rootPath}`;
            rootButton.setAttribute('aria-pressed', 'false');
            options.appendChild(rootButton);
            const lineBreak = document.createElement('span');
            lineBreak.className = 'output-dir-line-break';
            lineBreak.setAttribute('aria-hidden', 'true');
            options.appendChild(lineBreak);
        }
        const hasSavedPath =
            savedIsRoot ||
            directories.some((directory) => directory.path === savedPath);
        let selectedButton = rootButton;

        if (!directories.length) {
            if (rootButton) {
                this.selectOutputDirectory(rootButton, kind, false);
            } else {
                outputDirInput.value = '';
                const message = document.createElement('span');
                message.className = 'output-dir-message';
                message.textContent = 'No folders are available in the configured directory.';
                options.appendChild(message);
            }
            return;
        }

        directories.forEach((directory, index) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'output-dir-button';
            button.dataset.outputDir = directory.path;
            button.textContent = directory.name;
            button.title = `Select folder: ${directory.path}`;
            button.setAttribute('aria-pressed', 'false');
            options.appendChild(button);

            if (
                !savedIsRoot &&
                (directory.path === savedPath || (!hasSavedPath && index === 0))
            ) {
                selectedButton = button;
            }
        });
        if (selectedButton) {
            this.selectOutputDirectory(
                selectedButton,
                kind,
                Boolean(savedPath) && !hasSavedPath
            );
        }
    }

    getParentPath(path) {
        const trimmed = path.replace(/[\\/]+$/, '');
        const index = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
        if (index < 0) return null;
        if (index === 0) return trimmed.length > 1 ? trimmed.slice(0, 1) : null;
        const parent = trimmed.slice(0, index);
        // Keep drive roots such as "C:" usable as "C:\"
        return /^[A-Za-z]:$/.test(parent) ? parent + trimmed[index] : parent;
    }

    setPreferredOutputDirectories(preferences) {
        this.preferredOutputDirectories = {
            audio: preferences.audio || null,
            video: preferences.video || null,
            watcher: preferences.watcher || null
        };
    }

    selectOutputDirectory(button, kind = 'audio', persist = true) {
        if (!button) return;
        const picker = UIManager.PICKERS[kind];
        const options = document.getElementById(picker.options);
        const outputDirInput = document.getElementById(picker.hidden);
        options.querySelectorAll('.output-dir-button').forEach((option) => {
            const isSelected = option === button;
            option.classList.toggle('selected', isSelected);
            option.setAttribute('aria-pressed', String(isSelected));
        });
        outputDirInput.value = button.dataset.outputDir;
        if (persist) {
            this.preferredOutputDirectories[kind] = button.dataset.outputDir;
            this.onOutputDirectorySelected?.(kind, button.dataset.outputDir);
        }
        const rootPath = document.getElementById(picker.rootInput).value;
        this.updatePathDisplay(
            rootPath,
            button.dataset.outputDir === rootPath ? '' : button.textContent,
            kind
        );
    }

    updatePathDisplay(rootPath, folderName = '', kind = 'audio') {
        const display = document.getElementById(UIManager.PICKERS[kind].display);
        display.replaceChildren(document.createTextNode(rootPath));
        if (!folderName) return;
        const separator = rootPath.includes('\\') && !rootPath.includes('/') ? '\\' : '/';
        if (!/[\\/]$/.test(rootPath)) {
            display.appendChild(document.createTextNode(separator));
        }
        const strong = document.createElement('strong');
        strong.textContent = folderName;
        display.appendChild(strong);
    }

    async setAppVersion(version) {
        const versionElement = document.getElementById('app-version');
        if (versionElement && version) {
            versionElement.textContent = `v${version}`;
        }
    }
}

UIManager.PICKERS = {
    audio: {
        options: 'output-dir-options',
        hidden: 'output-dir',
        display: 'media-root-display',
        rootInput: 'media-root-input',
        rootKey: 'spotifysaver_media_root'
    },
    video: {
        options: 'video-output-dir-options',
        hidden: 'video-output-dir',
        display: 'video-media-root-display',
        rootInput: 'video-media-root-input',
        rootKey: 'spotifysaver_video_media_root'
    },
    watcher: {
        options: 'watcher-output-dir-options',
        hidden: 'watcher-output-dir',
        display: 'watcher-media-root-display',
        rootInput: 'watcher-media-root-input',
        rootKey: 'spotifysaver_watcher_media_root',
        mediaType: 'video'
    }
};