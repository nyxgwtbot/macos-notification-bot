<template>
  <ion-app>
    <ion-tabs>
      <ion-router-outlet />
      <ion-tab-bar slot="top" class="nb-nav">
        <ion-tab-button tab="chat" href="/chat">
          <ion-icon aria-hidden="true" :icon="chatbubblesOutline" />
          <ion-label>Chat</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="dashboard" href="/dashboard">
          <ion-icon aria-hidden="true" :icon="gridOutline" />
          <ion-label>Status</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="logs" href="/logs">
          <ion-icon aria-hidden="true" :icon="terminalOutline" />
          <ion-label>Logs</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="config" href="/config">
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
</script>

<style scoped>
.nb-nav {
  height: 44px;
  min-height: 44px;
  border-bottom: 1px solid var(--nb-border-default);
}

.nb-nav ion-tab-button {
  flex-direction: row;
  gap: var(--nb-space-2);
  min-height: 44px;
  height: 44px;
}

.nb-nav ion-tab-button ion-icon {
  font-size: 18px;
}
</style>
