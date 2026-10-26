<template>
  <ion-app>
    <ion-tabs>
      <ion-router-outlet />
      <ion-tab-bar slot="bottom">
        <ion-tab-button tab="dashboard" href="/dashboard">
          <ion-icon aria-hidden="true" :icon="gridOutline" />
          <ion-label>Painel</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="finance" href="/finance">
          <ion-icon aria-hidden="true" :icon="cashOutline" />
          <ion-label>Finanças</ion-label>
        </ion-tab-button>
        <ion-tab-button tab="config" href="/config">
          <ion-icon aria-hidden="true" :icon="settingsOutline" />
          <ion-label>Config</ion-label>
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
import { cashOutline, gridOutline, settingsOutline, terminalOutline } from 'ionicons/icons';
</script>