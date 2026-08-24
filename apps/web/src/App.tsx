import { Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { BenchmarksPage, TargetsPage } from './pages/CatalogPages';
import { BenchmarkRegistrationPage } from './pages/BenchmarkRegistrationPage';
import { CreateExperimentPage } from './pages/CreateExperimentPage';
import { CloudMarketPage } from './pages/CloudMarketPage';
import { CloudOrdersPage } from './pages/CloudOrdersPage';
import { CloudQuotePage } from './pages/CloudQuotePage';
import { CapacityListPage } from './pages/CapacityListPage';
import { CapacityStudyPage } from './pages/CapacityStudyPage';
import { CreateCapacityPage } from './pages/CreateCapacityPage';
import { DashboardPage } from './pages/DashboardPage';
import { ExperimentDetailPage } from './pages/ExperimentDetailPage';
import { ExperimentsPage } from './pages/ExperimentsPage';
import { SourceDiscoveryPage } from './pages/SourceDiscoveryPage';

export function App(){return <Routes><Route element={<Layout/>}><Route index element={<DashboardPage/>}/><Route path="experiments" element={<ExperimentsPage/>}/><Route path="experiments/new" element={<CreateExperimentPage/>}/><Route path="experiments/:id" element={<ExperimentDetailPage/>}/><Route path="benchmarks" element={<BenchmarksPage/>}/><Route path="benchmarks/register" element={<BenchmarkRegistrationPage/>}/><Route path="benchmarks/register/:registrationId" element={<BenchmarkRegistrationPage/>}/><Route path="targets" element={<TargetsPage/>}/><Route path="interfaces" element={<SourceDiscoveryPage/>}/><Route path="capacity" element={<CapacityListPage/>}/><Route path="capacity/new/:discoveryId" element={<CreateCapacityPage/>}/><Route path="capacity/:studyId" element={<CapacityStudyPage/>}/><Route path="cloud/market" element={<CloudMarketPage/>}/><Route path="cloud/quotes/:id" element={<CloudQuotePage/>}/><Route path="cloud/orders" element={<CloudOrdersPage/>}/><Route path="cloud/orders/:id" element={<CloudOrdersPage/>}/><Route path="*" element={<Navigate to="/" replace/>}/></Route></Routes>}
