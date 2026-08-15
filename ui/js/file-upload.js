/* ========================================
   file-upload.js — 文件上传管理（含拖拽）
   ======================================== */
(function (global) {
    'use strict';

    const FileUpload = {
        pendingFiles: [],
        fileInput: null,
        previewList: null,
        dropZone: null,

        init() {
            const attachBtn = Utils.$('#btn-attachment');
            this.fileInput = Utils.$('#file-input');
            this.previewList = Utils.$('#file-preview-list');
            this.dropZone = Utils.$('.input-wrapper');

            if (attachBtn && this.fileInput) {
                attachBtn.addEventListener('click', () => this.fileInput.click());
                this.fileInput.addEventListener('change', (e) => this._handleSelect(e));
            }

            // ★ 拖拽上传
            if (this.dropZone) {
                ['dragover', 'dragenter'].forEach(evt => {
                    this.dropZone.addEventListener(evt, (e) => {
                        e.preventDefault();
                        this.dropZone.classList.add('drag-over');
                    });
                });
                ['dragleave', 'dragend'].forEach(evt => {
                    this.dropZone.addEventListener(evt, () => {
                        this.dropZone.classList.remove('drag-over');
                    });
                });
                this.dropZone.addEventListener('drop', (e) => {
                    e.preventDefault();
                    this.dropZone.classList.remove('drag-over');
                    const files = Array.from(e.dataTransfer.files);
                    this._addFiles(files);
                });
            }

            Events.on('conversation:switched', () => {
                this.pendingFiles = [];
                this._renderPreview();
            });
        },

        _addFiles(files) {
            files.forEach(f => {
                if (this.pendingFiles.some(pf => pf.name === f.name && pf.size === f.size)) return;
                this.pendingFiles.push(f);
            });
            this._renderPreview();
        },

        _handleSelect(e) {
            const files = Array.from(e.target.files);
            this._addFiles(files);
            if (this.fileInput) this.fileInput.value = '';
        },

        _renderPreview() {
            if (!this.previewList) return;
            this.previewList.innerHTML = '';
            this.pendingFiles.forEach((file, idx) => {
                const item = Utils.create('div', { class: 'file-preview-item' }, [
                    Utils.create('span', { class: 'file-icon', text: this._getIcon(file.name) }),
                    Utils.create('span', { class: 'file-name', text: file.name }),
                    Utils.create('span', { class: 'file-remove', text: '×', title: '移除' })
                ]);
                const removeBtn = Utils.$('.file-remove', item);
                if (removeBtn) {
                    removeBtn.addEventListener('click', () => {
                        this.pendingFiles.splice(idx, 1);
                        this._renderPreview();
                    });
                }
                this.previewList.appendChild(item);
            });
        },

        _getIcon(filename) {
            const ext = filename.split('.').pop().toLowerCase();
            const icons = {
                pdf: '📕', doc: '📘', docx: '📘', xls: '📗', xlsx: '📗',
                png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', webp: '🖼️',
                txt: '📄', md: '📄', json: '📄', csv: '📊',
                py: '🐍', js: '📜', html: '🌐', css: '🎨'
            };
            return icons[ext] || '📎';
        },

        async uploadPending() {
            if (this.pendingFiles.length === 0) return [];
            const convId = State.currentConversationId;
            const uploaded = [];
            for (const file of this.pendingFiles) {
                const formData = new FormData();
                formData.append('file', file);
                try {
                    const resp = await fetch(`/conversations/${convId}/upload`, {
                        method: 'POST',
                        body: formData
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        uploaded.push(data);
                        Toast.success(`已上传：${data.filename}`);
                    } else {
                        Toast.error(`上传失败：${file.name}`);
                    }
                } catch (e) {
                    console.error('Upload error:', e);
                    Toast.error(`上传出错：${file.name}`);
                }
            }
            this.pendingFiles = [];
            this._renderPreview();
            return uploaded;
        },

        async loadFiles() {
            const convId = State.currentConversationId;
            try {
                const resp = await fetch(`/conversations/${convId}/files`);
                const data = await resp.json();
                return data.files || [];
            } catch (e) {
                console.warn('Failed to load files:', e);
                return [];
            }
        },

        async deleteFile(filename) {
            const convId = State.currentConversationId;
            try {
                await fetch(`/conversations/${convId}/files/${encodeURIComponent(filename)}`, {
                    method: 'DELETE'
                });
                Toast.info(`已删除：${filename}`);
            } catch (e) {
                console.error('Delete file error:', e);
            }
        }
    };

    global.FileUpload = FileUpload;
})(window);