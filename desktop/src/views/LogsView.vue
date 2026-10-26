<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
        <ion-title>Logs</ion-title>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <div v-if="store.logsError" class="nb-banner nb-banner-err">
        <span>Erro ao ler o log: {{ store.logsError }}</span>
        <ion-button size="small" fill="outline" @click="refreshOnce">Tentar de novo</ion-button>
      </div>

      <div class="nb-log-header">
        <span class="nb-muted">{{ logTitle }}</span>
        <ion-badge :color="store.feedError ? 'danger' : 'primary'">
          <ion-icon aria-hidden="true" :icon="pulseOutline" />
          auto-recarga 2s
        </ion-badge>
      </div>

      <p v-if="!store.logFileExists && !store.logsError" class="nb-empty">
        <code>logs/events.jsonl</code> ainda não existe — rode o bot (<code>python3 bot.py</code>) ou
        <code>python3 bot.py --once</code> para gerar o primeiro evento.
      </p>

      <div ref="logScroller" class="nb-log-scroller" @scroll="onScroll">
        <div
          v-for="line in store.logLines"
          :key="line.line_no"
          class="nb-line"
          :class="{ 'nb-line-bad': line.malformed }"
        >
          <button class="nb-copy" :aria-label="'copiar linha ' + line.line_no" @click="copyLine(line.raw)">
            <ion-icon aria-hidden="true" :icon="copyOutline" />
          </button>
        <pre class="nb-raw">{{ line.raw || ' ' }}</pre>
        <span v-if="copiedAt === line.raw" class="nb-copied">copiado</span>
        </div>
        <div v-if="store.logLines.length === 0 && store.logFileExists" class="nb-empty">log vazio</div>
      </div>
      <p v-if="copiedAllText" class="nb-muted nb-copy-note">{{ copiedAllText }}</p>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import {
  IonBadge,
  IonButton,
  IonContent,
  IonHeader,
  IonIcon,
  IonPage,
  IonTitle,
  IonToolbar
} from '@ionic/vue';
import { copyOutline, pulseOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import { usePolling } from '@/composables/usePolling';

const store = useBotStore();

const logScroller = ref<HTMLElement | null>(null);
const stickToBottom = ref(true);
const copiedAt = ref<string | null>(null);

const logTitle = computed(() => `cauda bruta de ${store.logLines.length} linha(s)`);
const copiedAllText = ref<string | null>(null);

/** Keep auto-scroll pinned to the bottom unless the user scrolled up. */
function onScroll(event: Event): void {
  const el = event.target as HTMLElement;
  if (!el) return;
  stickToBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 32;
}

async function copyLine(raw: string): Promise<void> {
  const text = raw ?? '';
  let copied = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (err) {
      console.debug('clipboard API falhou', err);
    }
  }
  if (!copied) copied = copyViaTextarea(text);
  if (copied) {
    copiedAt.value = text;
    copiedAllText.value = null;
    window.setTimeout(() => (copiedAt.value = null), 1200);
  }
}

function copyViaTextarea(text: string): boolean {
  const helper = document.createElement('textarea');
  helper.value = text;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.opacity = '0';
  document.body.appendChild(helper);
  helper.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    console.debug('execCommand copy falhou', err);
    ok = false;
  } finally {
    document.body.removeChild(helper);
  }
  if (!ok) copiedAllText.value = 'Cópia indisponível neste contexto.';
  return ok;
}

function refreshOnce(): void {
  void store.pollLogs();
}

const logPoller = usePolling(() => store.pollLogs(), { intervalMs: 2000 });

onMounted(async () => {
  await store.pollLogs();
  logPoller.start();
  await nextTick();
  stickToBottom.value = true;
});

onUnmounted(() => {
  logPoller.stop();
});
</script>

<style scoped>
.nb-content {
  padding: 12px;
}
.nb-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 4px 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--nb-danger-muted, rgba(248, 81, 73, 0.15));
  color: var(--ion-color-danger);
}
.nb-log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 4px 4px 10px;
}
.nb-muted {
  color: var(--nb-text-secondary, var(--ion-color-medium));
  font-size: 13px;
}
.nb-empty {
  text-align: center;
  color: var(--nb-text-secondary, var(--ion-color-medium));
  padding: 24px 0;
}
.nb-log-scroller {
  max-height: calc(100vh - 240px);
  overflow-y: auto;
  border: 1px solid var(--nb-border-default, #2a3544);
  border-radius: 10px;
  background: var(--nb-bg-deep, #181f28);
  padding: 8px 4px;
}
.nb-line {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.nb-line-bad .nb-raw {
  opacity: 0.38;
  font-style: italic;
}
.nb-raw {
  margin: 0;
  padding: 3px 40px 3px 8px;
  font-family: var(--nb-mono, monospace);
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--nb-text-primary, var(--ion-text-color));
}
.nb-copy {
  background: none;
  border: none;
  color: var(--nb-text-muted, var(--ion-color-medium));
  cursor: pointer;
  padding: 2px;
  margin-top: 2px;
}
.nb-copied {
  position: relative;
  right: 0;
  top: 3px;
  font-size: 10px;
  color: var(--ion-color-success);
}
.nb-copy-note {
  margin-top: 6px;
  font-size: 12px;
}
</style>