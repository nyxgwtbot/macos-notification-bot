<template>
  <ion-app>
    <ion-tabs>
      <ion-router-outlet />
      <ion-tab-bar slot="top" class="nb-nav">
        <!-- R8: the active button carries aria-current="page" (real current-page
             semantics for AT; Ionic's own tab role/aria-selected stay in place) -->
        <ion-tab-button tab="chat" href="/chat" layout="icon-start" :aria-current="route.path === '/chat' ? 'page' : undefined">
          <ion-icon aria-hidden="true" :icon="chatbubblesOutline" />
          <ion-label>Chat</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="dashboard" href="/dashboard" layout="icon-start" :aria-current="route.path === '/dashboard' ? 'page' : undefined">
          <ion-icon aria-hidden="true" :icon="gridOutline" />
          <ion-label>Status</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="logs" href="/logs" layout="icon-start" :aria-current="route.path === '/logs' ? 'page' : undefined">
          <ion-icon aria-hidden="true" :icon="terminalOutline" />
          <ion-label>Logs</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="config" href="/config" layout="icon-start" :aria-current="route.path === '/config' ? 'page' : undefined">
          <ion-icon aria-hidden="true" :icon="settingsOutline" />
          <ion-label>Settings</ion-label>
        </ion-tab-button>
      </ion-tab-bar>
    </ion-tabs>
  </ion-app>
</template>

<script setup lang="ts">
import { IonApp, IonRouterOutlet, IonTabBar, IonTabButton, IonIcon, IonLabel, IonTabs } from '@ionic/vue';
import { chatbubblesOutline, gridOutline, settingsOutline, terminalOutline } from 'ionicons/icons';
import { useRoute } from 'vue-router';

// R8 shared shell: the tab bar tracks the router's current route so the
// ACTIVE tab button gets aria-current="page" and the 2px teal underline:
// two consumers of one truth (no per-tab state drift).
const route = useRoute();
</script>

<style scoped>
.nb-nav {
  height: 44px;
  min-height: 44px;
  border-bottom: 1px solid var(--nb-border-default);
}

/* X2: label+icon pairs get one micro-gap and inline page padding.
   The icon/label gap lives on the shadow button (part=native) — the host-level
   gap alone never reaches it. */
.nb-nav ion-tab-button {
  gap: var(--nb-space-2);
  padding-inline: var(--nb-space-3);
  min-height: 44px;
  height: 44px;
}

.nb-nav ion-tab-button::part(native) {
  flex-direction: row;
  align-items: center;
  justify-content: center;
  gap: var(--nb-space-2);
}

.nb-nav ion-tab-button ion-icon {
  font-size: 18px;
  margin-inline-end: var(--nb-space-2);
}

/* R8 active tab indicator: a 2px teal underline on the selected button's
   native part (border-box — no layout shift), in the same teal the selected
   label already wears (--ion-tab-bar-color-selected: var(--nb-teal)). Ionic
   marks the selection on the host (.tab-selected); ::part(native) reaches
   the shadow button where the border must live. */
.nb-nav ion-tab-button.tab-selected::part(native) {
  border-bottom: 2px solid var(--nb-teal);
}
</style>
