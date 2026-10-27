<template>
  <ion-app>
    <ion-tabs>
      <ion-router-outlet />
      <ion-tab-bar slot="bottom">
        <ion-tab-button tab="dashboard" href="/dashboard">
          <ion-icon aria-hidden="true" :icon="gridOutline" />
          <ion-label>Dashboard</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="finance" href="/finance">
          <ion-icon aria-hidden="true" :icon="cashOutline" />
          <ion-label>Finance</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="chat" href="/chat">
          <ion-icon aria-hidden="true" :icon="chatbubblesOutline" />
          <ion-label>Chat</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="config" href="/config">
          <ion-icon aria-hidden="true" :icon="settingsOutline" />
          <ion-label>Settings</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="logs" href="/logs">
          <ion-icon aria-hidden="true" :icon="terminalOutline" />
          <ion-label>Logs</ion-label>
        </ion-tab-button>
      </ion-tab-bar>
    </ion-tabs>
  </ion-app>
</template>

<script setup lang="ts">
import { IonApp, IonRouterOutlet, IonTabBar, IonTabButton, IonIcon, IonLabel, IonTabs } from '@ionic/vue';
import { cashOutline, chatbubblesOutline, gridOutline, settingsOutline, terminalOutline } from 'ionicons/icons';
</script>