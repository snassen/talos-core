-- Messages filtered by sender domain: Overview → a domain → Messages with its senders.
--
-- The domain filter is split_part(from_address, '@', 2) = <domain>, which no index served, so
-- every domain click read the whole message table, five times: the list, the sender sidebar
-- and the type, topic and label counts ask at once. On 255k invented messages (249 MB) one
-- such render took 225 ms for a domain of 243 messages and 340 ms for one of 102k; with this
-- index, 8 ms and 190 ms.
create index if not exists message_from_domain_idx on message (split_part(from_address, '@', 2));
