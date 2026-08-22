import { Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { BenchmarksPage, TargetsPage } from './pages/CatalogPages';
import { BenchmarkRegistrationPage } from './pages/BenchmarkRegistrationPage';
import { CreateExperimentPage } from './pages/CreateExperimentPage';
import { CloudMarketPage } from './pages/CloudMarketPage';
import { CloudOrdersPage } from './pages/CloudOrdersPage';
import { CloudQuotePage } from './pages/CloudQuotePage';
import { DashboardPage } from './pages/DashboardPage';
import { ExperimentDetailPage } from './pages/ExperimentDetailPage';
import { ExperimentsPage } from './pages/ExperimentsPage';

export function App(){return <Routes><Route element={<Layout/>}><Route index element={<DashboardPage/>}/><Route path="experiments" element={<ExperimentsPage/>}/><Route path="experiments/new" element={<CreateExperimentPage/>}/><Route path="experiments/:id" element={<ExperimentDetailPage/>}/><Route path="benchmarks" element={<BenchmarksPage/>}/><Route path="benchmarks/register" element={<BenchmarkRegistrationPage/>}/><Route path="targets" element={<TargetsPage/>}/><Route path="cloud/market" element={<CloudMarketPage/>}/><Route path="cloud/quotes/:id" element={<CloudQuotePage/>}/><Route path="cloud/orders" element={<CloudOrdersPage/>}/><Route path="cloud/orders/:id" element={<CloudOrdersPage/>}/><Route path="*" element={<Navigate to="/" replace/>}/></Route></Routes>}
