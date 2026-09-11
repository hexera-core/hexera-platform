import { BigQuery } from "@google-cloud/bigquery";
import { CloudBillingClient } from "@google-cloud/billing";
import { BudgetServiceClient } from "@google-cloud/billing-budgets";
import {
  AutoscalersClient,
  InstanceGroupManagersClient,
  InstanceTemplatesClient,
} from "@google-cloud/compute";
import { MetricServiceClient } from "@google-cloud/monitoring";
import { ServicesClient } from "@google-cloud/run";

// The clients, and the NARROW TYPES the readers are written against.
//
// Every reader in this directory takes its client as a parameter typed as one of the `Pick<>`
// aliases below, never as the concrete class. That is what lets the tests hand over a four-line
// object literal instead of standing up an SDK client with fake credentials - and it is why no
// test in this directory can accidentally reach the network.
//
// The clients are memoised at module scope because a Cloud Run container serves many requests and
// each client opens its own auth and connection pool. Building one per render would spend a token
// exchange on every page load.

export type ManagerReader = Pick<InstanceGroupManagersClient, "get" | "listManagedInstances">;
export type ManagerWriter = Pick<
  InstanceGroupManagersClient,
  "deleteInstances" | "recreateInstances" | "resize"
>;
export type AutoscalerReader = Pick<AutoscalersClient, "get">;
export type AutoscalerWriter = Pick<AutoscalersClient, "get" | "update">;
export type TemplateReader = Pick<InstanceTemplatesClient, "get">;
export type MetricsReader = Pick<MetricServiceClient, "listTimeSeries">;
export type RunReader = Pick<ServicesClient, "getService">;
export type RunWriter = Pick<ServicesClient, "getService" | "updateService">;

export type FleetClients = {
  autoscalers: AutoscalerReader;
  instanceGroupManagers: ManagerReader;
  instanceTemplates: TemplateReader;
};

export type FleetWriteClients = {
  autoscalers: AutoscalerWriter;
  // Reader AND writer: a scaling change has to read the group first to learn its autoscaler's
  // name, which is not the group's own.
  instanceGroupManagers: ManagerReader & ManagerWriter;
};

let compute: {
  autoscalers: AutoscalersClient;
  instanceGroupManagers: InstanceGroupManagersClient;
  instanceTemplates: InstanceTemplatesClient;
} | null = null;
let metricsReader: MetricServiceClient | null = null;
let runClient: ServicesClient | null = null;

function getCompute() {
  compute ??= {
    autoscalers: new AutoscalersClient(),
    instanceGroupManagers: new InstanceGroupManagersClient(),
    instanceTemplates: new InstanceTemplatesClient(),
  };
  return compute;
}

export function getFleetClients(): FleetClients {
  return getCompute();
}

export function getFleetWriteClients(): FleetWriteClients {
  return getCompute();
}

export function getMetricsReader(): MetricsReader {
  metricsReader ??= new MetricServiceClient();
  return metricsReader;
}

export function getRunReader(): RunReader {
  runClient ??= new ServicesClient();
  return runClient;
}

export function getRunWriter(): RunWriter {
  runClient ??= new ServicesClient();
  return runClient;
}

let billingClient: CloudBillingClient | null = null;
let budgetClient: BudgetServiceClient | null = null;
let bigqueryClient: BigQuery | null = null;

export function getBillingReader(): CloudBillingClient {
  billingClient ??= new CloudBillingClient();
  return billingClient;
}

export function getBudgetReader(): BudgetServiceClient {
  budgetClient ??= new BudgetServiceClient();
  return budgetClient;
}

export function getSpendReader(projectId: string): BigQuery {
  bigqueryClient ??= new BigQuery({ projectId });
  return bigqueryClient;
}
